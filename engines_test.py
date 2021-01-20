import json
import tqdm
import torch
import torch.nn as nn
import numpy as np
import sklearn.metrics as metrics
from torch.cuda import amp
from augments import mixup, cutmix, snapmix
import torch.nn.functional as F

def trainer_augment(loaders, model_params, model, criterion, val_criterion, optimizer, lr_scheduler, optimizer_params, training_params, save_path):
    start_epoch = training_params['start_epoch']
    total_epochs = training_params['num_epoch']
    device = training_params['device']
    device_ids = training_params['device_ids']
    augment_prob = model_params['special_augment_prob']

    model = nn.DataParallel(model, device_ids=device_ids)
    cuda = device.type != 'cpu'
    scaler = amp.GradScaler(enabled=cuda)
    ema = model_params['ema_model']

    print("Epochs: {}\n".format(total_epochs))
    best_epoch = 1
    best_acc = 0.0
    history = {
        "train": {"loss": [], "acc": []},
        "eval": {"loss": [], "acc": []},
        "lr": []
    }
    
    #num_layer = 340
    num_layer = 152
    
    for epoch in range(start_epoch, total_epochs + 1):
        
        if epoch <= training_params['warm_up']:
            training_params['TTA_time'] = 1
            ct = 0
            for param in model.parameters():
                ct += 1
                if ct <= num_layer - 2:
                    param.requires_grad = False

            for module in model.modules():
                if isinstance(module, nn.BatchNorm2d):
                    if hasattr(module, 'weight'):
                        module.weight.requires_grad_(False)
                    if hasattr(module, 'bias'):
                        module.bias.requires_grad_(False)
                    module.eval()
            print('-------------------------', ct)
        elif epoch == training_params['warm_up'] + 1:
            training_params['TTA_time'] = 1
            for param in model.parameters():
                param.requires_grad = True

            for module in model.modules():
                if isinstance(module, nn.BatchNorm2d):
                    if hasattr(module, 'weight'):
                        module.weight.requires_grad_(True)
                    if hasattr(module, 'bias'):
                        module.bias.requires_grad_(True)
                    module.train()
        
        
        epoch_save_path = save_path + '_epoch-{}.pt'.format(epoch)
        head = "epoch {:2}/{:2}".format(epoch, total_epochs)
        print(head + "\n" + "-"*(len(head)))

        model.train()
        running_labels = []
        running_scores = []
        running_loss = 0.0
        optimizer.zero_grad()
        for images, labels in tqdm.tqdm(loaders["train"]):
            images, labels = images.to(device), labels.to(device)
            with amp.autocast(enabled=cuda):

                if np.random.rand(1) <= augment_prob:
                    running_labels = 0
                    if np.random.rand(1) >= 2:
                        SNAPMIX_ALPHA = 5.0
                        mixed_images, labels_1, labels_2, lam_a, lam_b = snapmix(images, labels, SNAPMIX_ALPHA, model)
                        mixed_images, labels_1, labels_2 = torch.autograd.Variable(mixed_images), torch.autograd.Variable(labels_1), torch.autograd.Variable(labels_2)
                        print(mixed_images.shape)
                        outputs, _ = model(mixed_images, train_state = True)
                        
                        loss_a = criterion(outputs, labels_1)
                        loss_b = criterion(outputs, labels_2)
                        loss = torch.mean(loss_a * lam_a + loss_b * lam_b)
                        #scores = torch.argmax(outputs, 1)
                        #print(labels_1.shape, labels_2.shape, lam_a, lam_b)
                        running_labels += labels_1.shape[0]
                        #running_labels += list(np.round(lam_a.item()*labels_1.unsqueeze(1).cpu().numpy() + lam_b.item()*labels_2.unsqueeze(1).cpu().numpy()))
                
                    else:
                        #print('Just Check')
                        mixed_images, labels_1, labels_2, lam = cutmix(images, labels)
                        mixed_images, labels_1, labels_2 = torch.autograd.Variable(mixed_images), torch.autograd.Variable(labels_1), torch.autograd.Variable(labels_2)

                        outputs, _ = model(mixed_images, train_state = True)
                        #scores = torch.argmax(outputs, 1)
                        loss = lam*criterion(outputs, labels_1.unsqueeze(1)) + (1 - lam)*criterion(outputs, labels_2.unsqueeze(1))
                        #scores = torch.argmax(outputs, 1)
                        #print(labels_1.shape, labels_2.shape)
                        running_labels += labels_1.shape[0]
                        #running_labels += list(np.round(lam*labels_1.unsqueeze(1).data.cpu().numpy() + (1 - lam)*labels_2.unsqueeze(1).data.cpu().numpy()))
                else:
                    #print('Damn')
                    outputs, _ = model(images)
                    scores = torch.argmax(outputs, 1)
                    loss = criterion(outputs, labels.unsqueeze(1))
                    #running_labels += labels.shape[0]
                    running_labels += list(labels.unsqueeze(1).data.cpu().numpy())
                    running_scores += list(scores.cpu().detach().numpy())
            
            scaler.scale(loss).backward()
            scaler.step(optimizer)  # optimizer.step
            scaler.update()
            optimizer.zero_grad()
            if ema:
                ema.update(model)
                
            
            running_loss += loss.item()*images.size(0)
        
        if running_labels:
            epoch_loss = running_loss/len(running_labels)
            print(np.array(running_labels).shape, np.array(running_scores).shape)
            epoch_accuracy_score = metrics.accuracy_score(running_labels, np.round(running_scores))
            history["train"]["loss"].append(epoch_loss)
            history["train"]["acc"].append(epoch_accuracy_score)
            print("{} - loss: {:.4f} - acc: {:.4f}".format("train", epoch_loss, epoch_accuracy_score))
        '''
        else:
        
        epoch_loss = running_loss/running_labels
        #epoch_accuracy_score = metrics.accuracy_score(running_labels, np.round(running_scores))
        history["train"]["loss"].append(epoch_loss)
        #history["train"]["acc"].append(epoch_accuracy_score)
        print("{} - loss: {:.4f}".format("train", epoch_loss))
        '''
        if ema:
            ema.update_attr(model, include=[])
            model_eval = ema.ema

        with torch.no_grad():
            if ema:
                # model_eval = model_eval.to(device)
                model_eval = nn.DataParallel(model_eval, device_ids=device_ids)
                model_eval.eval()
                # model_eval.half()
            else:
                model.eval()
            final_scores = []
            final_loss = 0.0

            for TTA in range(training_params['TTA_time']):
                running_labels = []
                running_scores = []
                running_outputs_softmax = np.empty((0,5))
                running_loss = 0.0

                for images, labels in tqdm.tqdm(loaders["eval"]):
                    images, labels = images.to(device), labels.to(device)

                    if ema:
                        outputs = model_eval(images)
                    else:
                        outputs, _ = model(images)
                    outputs = outputs[0]
                    outputs_softmax = F.log_softmax(outputs, dim=-1)
                    #print(outputs_softmax.shape, outputs.shape)
                    scores = torch.argmax(outputs_softmax, 1)
                    loss = val_criterion(outputs, labels.unsqueeze(1))

                    running_labels += list(labels.unsqueeze(1).data.cpu().numpy())
                    running_scores += list(scores.cpu().detach().numpy())
                    #print(running_outputs_softmax.shape, outputs_softmax.cpu().detach().numpy())
                    running_outputs_softmax = np.append(running_outputs_softmax, outputs_softmax.cpu().detach().numpy(), axis = 0)
                    #print(running_scores_softmax.shape, outputs_softmax.cpu().detach().numpy().shape, outputs_softmax.shape)
                    running_loss += loss.item()*images.size(0)
                print("{} - TTA loss: {:.4f} acc: {:.4f}".format("eval", 
                        running_loss/len(running_labels), metrics.accuracy_score(running_labels, 
                        np.round(running_scores))))

                if TTA == 0:
                    #final_scores = running_scores_softmax.copy()
                    final_scores = running_outputs_softmax
                    #print(final_scores.shape)
                else:
                    #final_scores += running_scores_softmax
                    final_scores += running_outputs_softmax
                final_loss += running_loss

        #epoch_loss = final_loss/(len(running_labels) * training_params['TTA_time'])
        final_scores_softmax_torch = torch.tensor(final_scores/training_params['TTA_time'], dtype=torch.float32)
        running_labels_torch = torch.tensor(running_labels, dtype=torch.float32)
        #print(final_scores_softmax_torch.shape, running_labels_torch.shape)
        
        epoch_loss = val_criterion(final_scores_softmax_torch, running_labels_torch, TTA = True)
        print(criterion(final_scores_softmax_torch, running_labels_torch, TTA = True))
        final_scores = np.argmax(final_scores, axis = 1)
        epoch_accuracy_score = metrics.accuracy_score(running_labels, np.round(final_scores))
        history["eval"]["loss"].append(epoch_loss.cpu().detach().numpy())
        history["eval"]["acc"].append(epoch_accuracy_score)
        print("{} loss: {:.4f} acc: {:.4f} lr: {:.9f}".format("eval - epoch", epoch_loss, epoch_accuracy_score, optimizer.param_groups[0]["lr"]))
        history["lr"].append(optimizer.param_groups[0]["lr"])
        lr_scheduler.step()

        if epoch_accuracy_score > best_acc:
            best_epoch = epoch
            best_acc = epoch_accuracy_score

        state_dicts = {
            "model_state_dict": model_eval.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "lr_scheduler_state_dict": lr_scheduler.state_dict(),
            "start_epoch": epoch + 1
        }

        torch.save(state_dicts, epoch_save_path)
    print("\nFinish: - Best Epoch: {} - Best accuracy: {}\n".format(best_epoch, best_acc))
    
    with open("{}.json".format(epoch_save_path[:-3]), "w") as f:
        json.dump(history, f)
    

def predicter(loader, model, device, device_ids, checkpoint_path):
    checkpoint = torch.load(checkpoint_path, map_location=device)

    model = model.to(device)
    model = nn.DataParallel(model, device_ids=device_ids)
    model.load_state_dict(checkpoint["model_state_dict"])

    with torch.no_grad():
        model.eval()
        running_labels = []
        running_scores = []
        for images, labels in tqdm.tqdm(loader):
            images, labels = images.to(device), labels.to(device)

            outputs, _ = model(images)
            scores = torch.sigmoid(outputs)

            running_labels += list(labels.unsqueeze(1).data.cpu().numpy())
            running_scores += list(scores.cpu().detach().numpy())

    return running_scores, running_labels


    