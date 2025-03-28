# class-wise scores
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset
from model.resnet import ResNet18
from model.vgg16 import VGG16
from model.TNet import TNet
import torch.nn.functional as F
import numpy as np
import math
import os
import random
import setproctitle
from ffcv.loader import Loader, OrderOption
from ffcv.transforms import ToTensor, ToDevice, Squeeze, RandomHorizontalFlip, RandomResizedCrop, RandomBrightness, RandomContrast, RandomSaturation
from ffcv.fields.decoders import IntDecoder, RandomResizedCropRGBImageDecoder
import argparse
from torchvision import transforms
import matplotlib.pyplot as plt
import gc
from torch.amp import autocast
import copy
from torch import Tensor
from typing import Callable
import concurrent.futures
import torch.multiprocessing as mp
from tqdm import tqdm
from sklearn.decomposition import PCA
import matplotlib.pyplot as plt
import seaborn as sns
from util.cal import get_acc, calculate_asr, compute_class_accuracy, compute_infoNCE, dynamic_early_stop
from util.plot import plot_and_save_mi, plot_train_acc_ASR, plot_train_loss_by_class
import wandb
from sklearn.manifold import TSNE

proc_name = 'lover'
setproctitle.setproctitle(proc_name)


# train one epoch
def train_loop(dataloader, model, loss_fn, optimizer, num_classes):
    size, num_batches = dataloader.batch_size, len(dataloader)
    model.train()
    epoch_acc = 0.0
    class_losses = torch.zeros(num_classes).to(next(model.parameters()).device)
    class_counts = torch.zeros(num_classes).to(next(model.parameters()).device)

    # 收集数据
    # 预分配张量存储数据
    total_samples = 50000
    x = torch.zeros((total_samples, 3, 32, 32), device=device)
    t = torch.zeros((total_samples, 512), device=device)  # 特征维度为512
    y = torch.zeros((total_samples, 10), device=device)
    labels_all = torch.zeros((total_samples), dtype=torch.long, device=device)
    is_backdoor_all = torch.zeros((total_samples), dtype=torch.long, device=device)
    current_idx = 0

    # 注册钩子函数到最后一个 BasicBlock
    hook_handle = model.layer4[-1].register_forward_hook(hook)

    for batch, (X, Y, is_backdoor) in enumerate(dataloader):
        optimizer.zero_grad()
        pred = model(X)
        loss = loss_fn(pred, Y)
        loss.backward()
        optimizer.step()
        epoch_acc += get_acc(pred, Y)

        # 计算每个类别的损失
        for c in range(num_classes):
            mask = (Y == c)
            if mask.sum() > 0:
                class_losses[c] += loss_fn(pred[mask], Y[mask]).item() * mask.sum().item()
                class_counts[c] += mask.sum().item()
        
        with torch.no_grad():
            M_output = F.adaptive_avg_pool2d(last_conv_output, 1)
            M_output = M_output.view(M_output.shape[0], -1)
        
        batch_size = len(Y)
        end_idx = current_idx + batch_size

        x[current_idx:end_idx] = X
        t[current_idx:end_idx] = M_output
        y[current_idx:end_idx] = pred
        labels_all[current_idx:end_idx] = Y
        is_backdoor_all[current_idx:end_idx] = is_backdoor
        current_idx = end_idx
    
    # 在计算MI之前移除钩子
    hook_handle.remove()
    
    # 裁剪张量到实际大小
    x = x[:current_idx].detach()
    t = t[:current_idx].detach()
    y = y[:current_idx].detach()
    labels_all = labels_all[:current_idx]
    is_backdoor_all = is_backdoor_all[:current_idx]

    avg_acc = 100 * (epoch_acc / num_batches)
    
    # 计算每个类别的平均损失
    class_losses = class_losses / class_counts
    class_losses = class_losses.cpu().numpy()

    print(f'Train acc: {avg_acc:.2f}%')
    for c in range(num_classes):
        print(f'Class {c} loss: {class_losses[c]:.4f}')

    return avg_acc, class_losses, x, t, y, labels_all, is_backdoor_all

def test_loop(dataloader, model, loss_fn):
    # Set the models to evaluation mode - important for batch normalization and dropout layers
    # Unnecessary in this situation but added for best practices
    model.eval()
    size = dataloader.batch_size
    num_batches = len(dataloader)
    total = size*num_batches
    test_loss, correct = 0, 0

    # Evaluating the models with torch.no_grad() ensures that no gradients are computed during test mode
    # also serves to reduce unnecessary gradient computations and memory usage for tensors with requires_grad=True
    with torch.no_grad():
        for X, y in dataloader:
            pred = model(X)
            test_loss += loss_fn(pred, y).item()
            correct += (pred.argmax(1) == y).type(torch.float).sum().item()

    test_loss /= num_batches
    correct /= total
    print(f"Test Error: \n Accuracy: {(100 * correct):>0.1f}%, Avg loss: {test_loss:>8f} \n")
    return test_loss, (100 * correct)

# 定义钩子函数
def hook(module, input, output):
    global last_conv_output
    last_conv_output = output.detach()

def estimate_mi(device, flag, x, t, y, class_idx, EPOCHS=50, mode='infoNCE'):
    # LR = 1e-5
    initial_lr = 1e-3
    if flag == 'inputs-vs-outputs':
        # if class_idx in range(1, 10):
        #     initial_lr = 3e-4
        Y_dim, Z_dim = 512, 3072  # M的维度, X的维度
    elif flag == 'outputs-vs-Y':
        Y_dim, Z_dim = 10, 512  # Y的维度, M的维度
    else:
        raise ValueError('Not supported!')
    
    T = TNet(in_dim=Y_dim + Z_dim, hidden_dim=256).to(device)
    # T = torch.nn.DataParallel(T)  # 使用 DataParallel
    optimizer = torch.optim.AdamW(T.parameters(), lr=initial_lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=5, verbose=True)
    M = []

    batch_size = 128
    # 采样20%数据
    sample_size = max(500, int(0.5 * len(x)))
    num_samples = sample_size
    sampled_indices = np.random.choice(len(x), sample_size, replace=False)
    x_sampled = x[sampled_indices].to(device)
    t_sampled = t[sampled_indices].to(device, dtype=torch.float32)
    y_sampled = y[sampled_indices].to(device)

    x_sampled = x_sampled.view(x_sampled.shape[0], -1)
    t_sampled = t_sampled.view(t_sampled.shape[0], -1)
    y_sampled = y_sampled.view(y_sampled.shape[0], -1)

    # num_samples = len(x)
    num_batches = (num_samples + batch_size - 1) // batch_size
    # print(f"num_samples: {num_samples}, num_batches: {num_batches}")
    
    # 使用tqdm.tqdm而不是tqdm.auto，并设置position参数
    position = mp.current_process()._identity[0] if mp.current_process()._identity else 0
    progress_bar = tqdm(
        range(EPOCHS),
        desc=f"class {class_idx}",
        position=position,
        leave=True,
        ncols=100
    )
    
    for epoch in progress_bar:
        
        # sample_size = int(0.5 * 4500)
        # sampled_indices = np.random.choice(len(x), sample_size, replace=False)
        # x_sampled = x[sampled_indices].to(device)
        # t_sampled = t[sampled_indices].to(device, dtype=torch.float32)
        # y_sampled = y[sampled_indices].to(device)

        epoch_losses = []
        
        for batch_idx in range(num_batches):
            # 获取当前批次的索引
            start_idx = batch_idx * batch_size
            end_idx = min((batch_idx + 1) * batch_size, num_samples)
            if end_idx - start_idx < 2:
                continue
            
            # 获取当前批次的数据
            X_batch = x_sampled[start_idx:end_idx]
            M_batch = t_sampled[start_idx:end_idx]
            Y_batch = y_sampled[start_idx:end_idx]
            
            if flag == 'inputs-vs-outputs':
                # X_batch = X_batch.view(X_batch.shape[0], -1)
                with autocast(device_type="cuda"):
                    loss, _ = compute_infoNCE(T, M_batch, X_batch, num_negative_samples=128)
            elif flag == 'outputs-vs-Y':
                # Y_batch = Y_batch.view(Y_batch.shape[0], -1)
                with autocast(device_type="cuda"):
                    loss, _ = compute_infoNCE(T, Y_batch, M_batch, num_negative_samples=128)

            if math.isnan(loss.item()) or math.isinf(loss.item()):
                print(f"Skipping batch {batch_idx} due to invalid loss: {loss.item()}")
                continue
                
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(T.parameters(), 10)
            optimizer.step()
            
            epoch_losses.append(loss.item())
        
        if not epoch_losses:
            M.append(float('nan'))
            continue
        
        avg_loss = np.mean(epoch_losses)
        M.append(-avg_loss)
        
        # 更新进度条
        progress_bar.set_postfix({'mi_estimate': -avg_loss})
        
        # 更新学习率
        scheduler.step(avg_loss)
        
        # 提前停止检查
        if dynamic_early_stop(M, delta=1e-2):
            print(f'Early stopping at epoch {epoch + 1}')
            break
        
        # torch.cuda.empty_cache()
        # gc.collect()

    # 清理进度条
    progress_bar.close()
    return M


def estimate_mi_wrapper(args):
    base_args, flag, class_idx, EPOCHS, mode, x, t, y, labels, is_backdoor = args    
    device = torch.device(f"cuda:0" if torch.cuda.is_available() else "cpu")
    # if isinstance(class_idx, int) and int(class_idx) > 5:
    #     device = torch.device(f"cuda:1" if torch.cuda.is_available() else "cpu")

    backdoor_mask = (is_backdoor==1)
    clean_mask = (is_backdoor==0)&(labels==0)

    if class_idx=='0_sample':
        # Calculate the target sample size (same as other classes)
        class_1_mask = (labels == 1)
        target_sample_size = len(x[class_1_mask])

        # Calculate the ratio of backdoor to clean samples
        total_backdoor = len(x[backdoor_mask])
        total_clean = len(x[clean_mask])
        backdoor_ratio = total_backdoor / (total_backdoor + total_clean)

        # Calculate how many samples we need from each
        backdoor_samples = int(target_sample_size * backdoor_ratio)
        clean_samples = target_sample_size - backdoor_samples

        # Randomly sample indices
        backdoor_indices = torch.randperm(total_backdoor)[:backdoor_samples]
        clean_indices = torch.randperm(total_clean)[:clean_samples]

        x_ = torch.cat([
                    x[backdoor_mask][backdoor_indices],
                    x[clean_mask][clean_indices]
        ])
        t_ = torch.cat([
                    t[backdoor_mask][backdoor_indices],
                    t[clean_mask][clean_indices]
        ])
        y_ = torch.cat([
                    y[backdoor_mask][backdoor_indices],
                    y[clean_mask][clean_indices]
        ])
    else:
        if class_idx=='0_backdoor':
            mask = backdoor_mask
        elif class_idx=='0_clean':
            mask = clean_mask
        else:
            mask = (labels == class_idx)
        x_, t_, y_ = x[mask], t[mask], y[mask]
    
    return estimate_mi(device, flag, x_, t_, y_, class_idx, EPOCHS, mode)

def train(args, flag='inputs-vs-outputs', mode='infoNCE'):
    """ flag = inputs-vs-outputs or outputs-vs-Y """
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    batch_size = 512  
    learning_rate = 0.1

    # 动态设置 num_workers
    num_workers = 16

    # Data decoding and augmentation
    image_pipeline = [ToTensor(), ToDevice(device)]
    label_pipeline = [IntDecoder(), ToTensor(), ToDevice(device), Squeeze()]

    # Pipeline for each data field
    pipelines = {
        'image': image_pipeline,
        'label': label_pipeline,
        'is_backdoor': label_pipeline
    }

    test_pipelines = {
        'image': image_pipeline,
        'label': label_pipeline,
    }

    train_dataloader_path = args.train_data_path
    train_dataloader = Loader(train_dataloader_path, batch_size=batch_size, num_workers=num_workers,
                              order=OrderOption.RANDOM, os_cache=True, pipelines=pipelines, drop_last=False)

    test_dataloader_path = args.test_data_path
    test_dataloader = Loader(test_dataloader_path, batch_size=batch_size, num_workers=num_workers,
                             order=OrderOption.RANDOM, pipelines=test_pipelines, seed=0)
    
    test_poison_data = np.load("data/badnet/0.1/poisoned_test_data.npz")
    test_poison_dataset = TensorDataset(
        torch.tensor(test_poison_data['arr_0'], dtype=torch.float32).permute(0, 3, 1, 2),
        torch.tensor(test_poison_data['arr_1'], dtype=torch.long)
    )
    test_poison_dataloader = DataLoader(test_poison_dataset, batch_size=batch_size, shuffle=False, num_workers=0)
    

    num_classes = 10
    if args.model == 'resnet18':
        model = ResNet18(num_classes=num_classes, noise_std_xt=args.noise_std_xt, noise_std_ty=args.noise_std_ty)  
        model.conv1 = nn.Conv2d(in_channels=3, out_channels=64, kernel_size=3, stride=1, padding=1, bias=False)
        model.fc = torch.nn.Linear(512, num_classes) # 将最后的全连接层改掉
    elif args.model == 'vgg16':
        model = VGG16(num_classes=num_classes, noise_std_xt=args.noise_std_xt, noise_std_ty=args.noise_std_ty)
    # model = nn.DataParallel(model)  # 使用 DataParallel
    model.to(device)
    model.train()

    loss_fn = nn.CrossEntropyLoss()
    optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate, momentum=0.9, weight_decay=5e-4)
    
    # 使用 StepLR 调整学习率，每10个epoch，lr乘0.5
    # scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=10, gamma=0.5)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=5, verbose=True)

    best_accuracy = 0
    best_model = None
    epochs = 100
    MI_inputs_vs_outputs = {class_idx: [] for class_idx in args.observe_classes}
    MI_Y_vs_outputs = {class_idx: [] for class_idx in args.observe_classes}
    class_losses_list = []
    previous_test_loss = float('inf')

    # 初始化 wandb
    wandb.init(
        project="MI-Analysis",
        name=f"exp_{args.model}_xt{args.noise_std_xt}_ty{args.noise_std_ty}_{args.outputs_dir.split('/')[-2]}_{args.train_data_path.split('/')[-2]}",
        config={
            "model": args.model,
            "noise_std_xt": args.noise_std_xt,
            "noise_std_ty": args.noise_std_ty,
            "learning_rate": learning_rate,
            "batch_size": batch_size,
            "epochs": epochs,
            "num_workers": num_workers,
            "observe_classes": args.observe_classes,
            "train_data_path": args.train_data_path,
            "test_data_path": args.test_data_path
        }
    )

    for epoch in range(1, epochs + 1):
        print(f"------------------------------- Epoch {epoch} -------------------------------")
        train_acc, class_losses, x, t, y, labels, is_backdoor = train_loop(train_dataloader, model, loss_fn, optimizer, num_classes)
        test_loss, test_acc = test_loop(test_dataloader, model, loss_fn)
        _asr = calculate_asr(model, test_poison_dataloader, 0, device)       
        class_losses_list.append(class_losses)

        # Visualize t using t-SNE
        if epoch % 10 == 0:
            tsne = TSNE(n_components=2, random_state=42)
            t_tsne = tsne.fit_transform(t.cpu().numpy())
            
            plt.figure(figsize=(10, 8))
            sns.scatterplot(x=t_tsne[:, 0], y=t_tsne[:, 1], hue=labels.cpu().numpy(), palette='tab10', legend='full')
            plt.title(f't-SNE of t at Epoch {epoch}')
            plt.xlabel('t-SNE Component 1')
            plt.ylabel('t-SNE Component 2')
            plt.legend(title='Class')
            plt.savefig(os.path.join(args.outputs_dir, f'tsne_t_epoch_{epoch}.png'))
            plt.close()

        # 创建一个包含所有类别损失的图表
        wandb.log({
            "train_accuracy": train_acc,
            "test_accuracy": test_acc,
            "test_loss": test_loss,
            "attack_success_rate": _asr,
        }, step=epoch)

        # 保存最佳模型
        if test_acc > best_accuracy:
            best_accuracy = test_acc
            best_model = copy.deepcopy(model)
            print(f"New best model saved with accuracy: {best_accuracy:.2f}%")

        # 调整学习率
        scheduler.step(test_loss)
        
        # 检查是否应该计算互信息
        # should_compute_mi = ((t % pow(2, t//10) == 0) or t%10==0) and test_loss < previous_test_loss
        # should_compute_mi = (t % pow(2, t//10) == 0) and (test_loss < previous_test_loss if t < 10 else True)
        # should_compute_mi = test_loss < previous_test_loss
        # should_compute_mi = t==1 or t==8 or t==15 or t==25 or t==40 or t==60
        should_compute_mi = epoch in [1, 5, 10, 20, 40, 60, 80, 100]
        # should_compute_mi = epoch in [1, 3, 8, 10, 20, 30, 50]
        # should_compute_mi = epoch in [60, 100]
        # should_compute_mi = t==20 or t==80
        # should_compute_mi = False
        if should_compute_mi:
            print(f"------------------------------- Epoch {epoch} -------------------------------")
            mi_inputs_vs_outputs_dict = {}
            mi_Y_vs_outputs_dict = {}

            # 创建一个进程池
            with concurrent.futures.ProcessPoolExecutor(max_workers=len(args.observe_classes)) as executor:
                # 计算 I(X,T) 和 I(T,Y)
                compute_args = [(args, 'inputs-vs-outputs', class_idx, 400, mode, x, t, y, labels, is_backdoor) 
                                for class_idx in args.observe_classes]
                results_inputs_vs_outputs = list(executor.map(estimate_mi_wrapper, compute_args))

            # with concurrent.futures.ProcessPoolExecutor(max_workers=len(args.observe_classes)) as executor:    
            #     compute_args = [(args, 'outputs-vs-Y', class_idx, 200, mode, x, t, y, labels, is_backdoor) 
            #                     for class_idx in args.observe_classes]
            #     results_Y_vs_outputs = list(executor.map(estimate_mi_wrapper, compute_args))

            # 处理结果
            for class_idx, result in zip(args.observe_classes, results_inputs_vs_outputs):
                mi_inputs_vs_outputs = result
                mi_inputs_vs_outputs_dict[class_idx] = mi_inputs_vs_outputs
                MI_inputs_vs_outputs[class_idx].append(mi_inputs_vs_outputs)

            # for class_idx, result in zip(args.observe_classes, results_Y_vs_outputs):
            #     mi_Y_vs_outputs = result
            #     mi_Y_vs_outputs_dict[class_idx] = mi_Y_vs_outputs
            #     MI_Y_vs_outputs[class_idx].append(mi_Y_vs_outputs)

            # 保存 MI 图到 wandb
            plot_and_save_mi(mi_inputs_vs_outputs_dict, 'inputs-vs-outputs', args.outputs_dir, epoch)
            # plot_and_save_mi(mi_Y_vs_outputs_dict, 'outputs-vs-Y', args.outputs_dir, epoch)

            np.save(f'{args.outputs_dir}/infoNCE_MI_I(X,T).npy', MI_inputs_vs_outputs)
            # np.save(f'{args.outputs_dir}/infoNCE_MI_I(Y,T).npy', MI_Y_vs_outputs)
            
            # 上传图片到 wandb
            wandb.log({
                f"I(X;T)_estimation": wandb.Image(os.path.join(args.outputs_dir, f'mi_plot_inputs-vs-outputs_epoch_{epoch}.png')),
                # f"I(T;Y)_estimation": wandb.Image(os.path.join(args.outputs_dir, f'mi_plot_outputs-vs-Y_epoch_{epoch}.png'))
            }, step=epoch)

        # 更新前一个epoch的test_loss
        previous_test_loss = test_loss

    plot_train_loss_by_class(class_losses_list, epoch, num_classes, args.outputs_dir)
    wandb.log({
        "train_loss_by_class": wandb.Image(os.path.join(args.outputs_dir, 'train_loss_by_class_plot.png'))
    })

    wandb.finish()
    return MI_inputs_vs_outputs, MI_Y_vs_outputs, best_model


def ob_infoNCE(args):
    outputs_dir = args.outputs_dir
    if not os.path.exists(outputs_dir):
        os.makedirs(outputs_dir)
    infoNCE_MI_log_inputs_vs_outputs, infoNCE_MI_log_Y_vs_outputs, best_model = train(args, 'inputs-vs-outputs', 'infoNCE')
     
    # 保存最佳模型
    # torch.save(best_model, os.path.join(args.outputs_dir, 'best_model.pth'))

    # 检查并保存 infoNCE_MI_log_inputs_vs_outputs
    infoNCE_MI_log_inputs_vs_outputs = np.array(infoNCE_MI_log_inputs_vs_outputs, dtype=object)
    np.save(f'{outputs_dir}/infoNCE_MI_I(X,T).npy', infoNCE_MI_log_inputs_vs_outputs)
    print(f'saved in {outputs_dir}/infoNCE_MI_I(X,T).npy')
    
    # 检查并保存 infoNCE_MI_log_Y_vs_outputs
    # infoNCE_MI_log_Y_vs_outputs = np.array(infoNCE_MI_log_Y_vs_outputs, dtype=object)
    # np.save(f'{outputs_dir}/infoNCE_MI_I(Y,T).npy', infoNCE_MI_log_Y_vs_outputs)
    # print(f'saved in {outputs_dir}/infoNCE_MI_I(Y,T).npy')

if __name__ == '__main__':
    device = torch.device('cuda')
    mp.set_start_method('spawn', force=True)
    torch.manual_seed(0)
    parser = argparse.ArgumentParser()
    parser.add_argument('--outputs_dir', type=str, default='results/ob_infoNCE_06_22', help='output_dir')
    parser.add_argument('--sampling_datasize', type=str, default='1000', help='sampling_datasize')
    parser.add_argument('--training_epochs', type=str, default='100', help='training_epochs')
    parser.add_argument('--batch_size', type=str, default='256', help='batch_size')
    parser.add_argument('--learning_rate', type=str, default='1e-5', help='learning_rate')
    parser.add_argument('--mi_estimate_epochs', type=str, default='300', help='mi_estimate_epochs')
    parser.add_argument('--mi_estimate_lr', type=str, default='1e-6', help='mi_estimate_lr')
    parser.add_argument('--class', type=str, default='0', help='class')
    parser.add_argument('--train_data_path', type=str, default='0', help='path of training data')
    parser.add_argument('--test_data_path', type=str, default='0', help='path of test data')
    parser.add_argument('--test_poison_data_path', type=str, default="data/badnet/0.1/poisoned_test_data.npz", help='path of poisoned test data')
    parser.add_argument('--model', type=str, choices=['resnet18', 'vgg16'], default='resnet18', help='model architecture')
    parser.add_argument('--noise_std_xt', type=float, default=0.4, help='noise_std_xt')
    parser.add_argument('--noise_std_ty', type=float, default=0.6, help='noise_std_ty')
    # parser.add_argument('--observe_classes', type=list, default=[0,1,2,3,4,5,6,7,8,9], help='class')
    parser.add_argument('--observe_classes', type=list, default=[0,'0_backdoor','0_clean','0_sample',1,2,3,4,5,6,7,8,9], help='class')
    args = parser.parse_args()
    # ob_DV()
    ob_infoNCE(args)