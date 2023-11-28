from multimodal_breast_analysis.models.faster_rcnn import faster_rcnn
from multimodal_breast_analysis.models.unet import UNet as unet
from multimodal_breast_analysis.configs.configs import config

import wandb
import torch
from torch.optim import Adam, SGD
from torch.optim.lr_scheduler import StepLR, CyclicLR
from torch.nn import KLDivLoss, Flatten, Linear
from torch.nn.functional import softmax, log_softmax

class Engine:
    def __init__(self):
        self.device = torch.device(config.device if torch.cuda.is_available() else "cpu")
        self.student = self._get_model(
            config.networks["student"],
            config.networks["student_parameters"]
            )
        self.teacher = self._get_model(
            config.networks["teacher"],
            config.networks["teacher_parameters"]
            )    
        self.student_optimizer = self._get_optimizer(
            config.train["student_optimizer"]
        )(self.student.parameters(),**config.train["student_optimizer_parameters"])
        self.teacher_optimizer = self._get_optimizer(
            config.train["teacher_optimizer"]
        )(self.teacher.parameters(),**config.train["teacher_optimizer_parameters"])
        self.student_scheduler = self._get_scheduler(
            config.train["student_scheduler"],
        )(self.student_optimizer,**config.train["student_scheduler_parameters"])
        self.teacher_scheduler = self._get_scheduler(
            config.train["teacher_scheduler"],
        )(self.teacher_optimizer,**config.train["teacher_scheduler_parameters"])
        self.student_trainloader = None
        self.student_testloader = None
        self.teacher_trainloader = None
        self.teacher_testloader = None


    def _get_model(self, name, parameters):
        models = {
            "faster_rcnn": faster_rcnn,
            "unet": unet
        }
        return models[name][parameters]
    

    def _get_optimizer(self, name):
        optimizers = {
            "sgd": SGD,
            "adam": Adam
        }
        return optimizers[name]
   
        
    def _get_scheduler(self, name):
        schedulers = {
            "step" : StepLR,
            "cyclic" : CyclicLR,
        }
        return schedulers[name]
    

    def save(self, mode, path=None):
        if mode == 'teacher':
            if path is None:
                path = config.networks["teacher_cp"]
            checkpoint = {
                "network": self.teacher.state_dict(),
                "optimizer": self.teacher_optimizer.state_dict(),
                }
        elif mode == 'student':
            if path is None:
                path = config.networks["student_cp"]
            checkpoint = {
                "network": self.student.state_dict(),
                "optimizer": self.student_optimizer.state_dict(),
                }            
        torch.save(checkpoint, path=path)


    def load_checkpoint(self, mode, path):
        if mode == "teacher":
            if path is None:
                path = config.networks["teacher_cp"]
            checkpoint = torch.load(path)
            self.teacher.load_state_dict(checkpoint['network'])
            self.teacher_optimizer.load_state_dict(checkpoint['optimizer'])
        elif mode == "student":
            if path is None:
                path = config.networks["student_cp"]
            checkpoint = torch.load(path)
            self.student.load_state_dict(checkpoint['network'])
            self.student_optimizer.load_state_dict(checkpoint['optimizer'])


    def warmup(self):
        warmup_epochs = config.train["warmup_epochs"]
        best_metric = 0
        for epoch in range(warmup_epochs):
            print(f"\nWarmup Epoch {epoch+1}/{warmup_epochs}\n-------------------------------")
            epoch_loss = 0
            self.teacher.train()
            for iteration, batch in enumerate(self.teacher_trainloader):
                print(f"   iteration {iteration+1}/{len(self.teacher_trainloader)}")
                loss = self.teacher(
                            batch['image'].to(self.device),
                            batch['label'].to(self.device)
                            )
                loss = sum(sample_loss for sample_loss in loss.values())
                self.optimizer_ct.zero_grad()
                loss.backward()
                self.optimizer.step()
                epoch_loss += loss.item()
            epoch_loss = epoch_loss / len(self.teacher_trainloader)
            print("Warmup Loss:", epoch_loss)            
            current_metric = self.test('teacher')
            print("Warmup Metric:", current_metric)
            wandb.log({
                "teacher training loss": epoch_loss, 
                "teacher testing metric": current_metric
                })
            if current_metric >= best_metric:
                best_metric = current_metric
                print('saving teacher checkpoint')
                self.save('teacher')


    def distill_loss(student_outputs, teacher_outputs):
        T, alpha = config.train["temperature"], config.train["alpha"]
        return KLDivLoss(reduction='batchmean')(
            log_softmax(student_outputs/T, dim=1),
            softmax(teacher_outputs/T, dim=1)
            ) * (alpha * T * T)
    

    def _instantiate_kd(self):
        self.features = {}
        def get_features(name):
            def hook(model, input, output):
                self.features[name] = output
            return hook
        self.student.backbone.register_forward_hook(get_features('student'))
        self.teacher.backbone.register_forward_hook(get_features('teacher'))
        student_batch = next(iter(self.student_trainloader))
        teacher_batch = next(iter(self.teacher_trainloader))
        self.student(student_batch['image'].to(self.device))
        self.teacher(teacher_batch['image'].to(self.device))
        student_features = self.features['student']
        teacher_features = self.features['teacher']
        self.flat = Flatten(start_dim=2).to(self.device)
        self.project = Linear(
                            torch.prod(torch.tensor(student_features.shape[2:])),
                            torch.prod(torch.tensor(teacher_features.shape[2:]))
                            ).to(self.device)
        self.student_optimizer.add_param_group({'params':self.project.parameters()})


    def train(self):
        self._instantiate_kd()
        epochs = config.train["epochs"]
        distill_epoch = config.train["distill_epoch"]
        distill_coeff = config.train["distill_coeff"]
        distill_loss = torch.tensor(0)
        best_metric = 0 
        for epoch in range(epochs):
            print(f"\nEpoch {epoch+1}/{epochs}\n-------------------------------")
            epoch_total_loss = 0
            epoch_base_loss = 0
            epoch_distill_loss = 0
            self.student.train()
            self.teacher.eval()
            for iteration, student_batch in enumerate(self.student_trainloader):
                print(f"     iteration {iteration+1}/{len(self.student_trainloader)}")
                base_loss = self.student(
                                    student_batch['image'].to(self.device),
                                    student_batch['label'].to(self.device)
                                    )
                base_loss = sum(sample_loss for sample_loss in base_loss.values())
                if epoch >= distill_epoch:
                    try:
                        teacher_batch = next(teacher_iter)
                    except StopIteration:
                        teacher_iter = iter(self.teacher_trainloader)
                        teacher_batch = next(teacher_iter)
                        # if (len(teacher_batch['image']) != len(student_batch['image'])):
                        #     continue
                    self.teacher(teacher_batch['image'].to(self.device))
                    teacher_features = self.features['teacher']
                    student_features = self.features['student']
                    teacher_features = self.flat(teacher_features).mean(dim = 0)
                    student_features = self.project(self.flat(student_features).mean(dim = 0))
                    distill_loss = self.distill_loss(student_features, teacher_features)
                total_loss = base_loss + distill_coeff*distill_loss
                self.student_optimizer.zero_grad()
                total_loss.backward()
                self.student_optimizer.step()
                epoch_total_loss += total_loss.item()
                epoch_base_loss += base_loss.item()
                epoch_distill_loss += distill_loss.item()
                print(
                    "    Total Loss:", total_loss.item(), 
                    "Base:", base_loss.item(), 
                    "Distill:", distill_loss.item()
                    )
            epoch_total_loss = epoch_total_loss / len(self.student_trainloader)
            epoch_base_loss = epoch_base_loss / len(self.student_trainloader)
            epoch_distill_loss = epoch_distill_loss / len(self.student_trainloader)
            print(
                "Total Loss:", epoch_total_loss, 
                "Base:", epoch_base_loss, 
                "Distill:", epoch_distill_loss
                )
            current_metric = self.test('student')
            print("Metric:", current_metric)
            wandb.log({
                "student total training loss": epoch_total_loss,
                "student base training loss": epoch_base_loss,
                "student distill training loss": epoch_distill_loss, 
                "student testing metric": current_metric
                })
            if current_metric >= best_metric:
                best_metric = current_metric
                print('saving student checkpoint')
                self.save('student')


    def test(self, mode):
        if mode == "student":
            dataloader = self.student_testloader
            network = self.student
        elif mode == "teacher":
            dataloader = self.teacher_testloader
            network = self.teacher
        pass


    def predict(self):
        pass