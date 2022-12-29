import os
import random
import argparse
import yaml
from tqdm import tqdm
from io import BytesIO
import torch
import torch.nn.functional as F
import torch.nn as nn

from datasets.imagenet import ImageNet
import clip
from utils_vqa_tipadapter import *
import base64
from torchvision import transforms
from PIL import Image, ImageFile

IMAGENET_DEFAULT_MEAN = (0.485, 0.456, 0.406)
IMAGENET_DEFAULT_STD = (0.229, 0.224, 0.225)


def get_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/home/moming/code/Tip-Adapter/configs/vqa.yaml', dest='config',
                        help='settings of Tip-Adapter in yaml format')
    args = parser.parse_args()

    return args


def run_tip_adapter_F(cfg, cache_keys, cache_values, test_features, test_labels, clip_model, train_loader_F):
    
    # Enable the cached keys to be learnable
    device_id = clip_model.positional_embedding.device
    adapter = nn.Linear(cache_keys.shape[0], cache_keys.shape[1], bias=False).to(clip_model.dtype).to(device_id)
    clss_head = nn.Linear(512 * 5, 2, bias=False).to(clip_model.dtype)
    adapter.weight = nn.Parameter(cache_keys.t())
    adapter.to(device_id)
    clss_head.to(device_id)
    # adapter.weight = nn.Parameter(cache_keys)

    include = lambda n: "bias" in n
    named_parameters = list(clip_model.named_parameters())

    tune_params = [p for n, p in named_parameters if include(n) and p.requires_grad]
    fix_params = [p for n, p in named_parameters if not include(n)]
    optimizer = torch.optim.AdamW(
        [
            {"params": tune_params, 'lr': 0.0},
            {"params": fix_params, "lr": 0.0},
            {"params": adapter.parameters(), "lr": 5e-7},
            {"params": clss_head.parameters(), "lr": 2e-3}
        ],
        lr=cfg['lr'],
        betas=(0.9, 0.999),
        eps=1e-4,
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, cfg['train_epoch'] * len(train_loader_F))
    
    beta, alpha = cfg['init_beta'], cfg['init_alpha']
    best_acc, best_epoch = 0.0, 0

    for train_idx in range(cfg['train_epoch']):
        # Train
        adapter.train()
        clip_model.train()
        correct_samples, all_samples = 0, 0
        loss_list = []
        print('Train Epoch: {:} / {:}'.format(train_idx, cfg['train_epoch']))

        for i, (images, questions, target) in enumerate(tqdm(train_loader_F)):
            target = target.to(device_id)

            image_features = encode_img(clip_model, images, device_id)
            hypo_feature = encode_txt(clip, clip_model, questions, device_id)
            adapter_weights = fusion_feat(hypo_feature, image_features)

            affinity = adapter(adapter_weights)
            cache_logits = ((-1) * (beta - beta * affinity)).exp() @ cache_values
            clss_logits = clss_head(adapter_weights)
            cache_logits = clss_logits + alpha * cache_logits
            loss = F.cross_entropy(cache_logits, target)

            acc = cls_acc(cache_logits, target)
            # acc = torch.sum((equal_prob < 0.5) == target) / target.size(0)
            correct_samples += acc / 100 * len(cache_logits)
            all_samples += len(cache_logits)
            loss_list.append(loss.item())

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()

        current_lr = scheduler.get_last_lr()[0]
        print('LR: {:.6f}, Acc: {:.4f} ({:}/{:}), Loss: {:.4f}'.format(current_lr, correct_samples / all_samples, correct_samples, all_samples, sum(loss_list)/len(loss_list)))

        # Eval
        adapter.eval()
        clip_model.eval()

        affinity = adapter(test_features)
        cache_logits = ((-1) * (beta - beta * affinity)).exp() @ cache_values
        clss_logits = clss_head(test_features)
        cache_logits = alpha * cache_logits + clss_logits
        acc = cls_acc(cache_logits, test_labels)

        print("**** Tip-Adapter-F's test accuracy: {:.2f}. ****\n".format(acc))
        if acc > best_acc:
            best_acc = acc
            best_epoch = train_idx
            torch.save(adapter.weight, cfg['cache_dir'] + "/best_F_" + str(cfg['shots']) + "shots.pt")
    
    adapter.weight = torch.load(cfg['cache_dir'] + "/best_F_" + str(cfg['shots']) + "shots.pt")
    print(f"**** After fine-tuning, Tip-Adapter-F's best test accuracy: {best_acc:.2f}, at epoch: {best_epoch}. ****\n")

    # Search Hyperparameters
    # _ = search_hp(cfg, None, cache_values, test_features, test_labels, adapter=adapter)

def load_data_file(data_file):
    train_path = data_file % "train"
    dev_path = data_file % "val"
    train_lines = open(train_path, 'r').readlines()
    dev_lines = open(dev_path, 'r').readlines()
    return train_lines, dev_lines

def main():

    # Load config file
    args = get_arguments()
    assert (os.path.exists(args.config))
    
    cfg = yaml.load(open(args.config, 'r'), Loader=yaml.Loader)

    cache_dir = os.path.join('./caches', cfg['dataset'])
    os.makedirs(cache_dir, exist_ok=True)
    cfg['cache_dir'] = cache_dir

    print("\nRunning configs.")
    print(cfg, "\n")

    # CLIP
    clip_model, preprocess = clip.load(cfg['backbone'], "cuda:3")
    # clip_model.eval()

    # ImageNet dataset
    random.seed(1)
    torch.manual_seed(1)
    
    print("Preparing VQA dataset.")
    train_datasets, dev_datasets = load_data_file(cfg['root_path'])
    train_datasets = train_datasets[:2000]
    mean = IMAGENET_DEFAULT_MEAN
    std = IMAGENET_DEFAULT_STD
    patch_image_size = 224
    # mean = [0.5, 0.5, 0.5]
    # std = [0.5, 0.5, 0.5]
    patch_resize_transform = transforms.Compose([
        lambda image: image.convert("RGB"),
        transforms.Resize((patch_image_size, patch_image_size), interpolation=Image.Resampling.BICUBIC),
        transforms.ToTensor(),
        transforms.Normalize(mean=mean, std=std),
    ])

    def process_image_from_ofa(samples):
        images = []
        questions = []
        labels = []
        lines = [s.split('\t') for s in samples]
        for l in lines:
            img_str = l[4]
            image = Image.open(BytesIO(base64.urlsafe_b64decode(img_str)))
            image = patch_resize_transform(image)
            images.append(image.unsqueeze(0))
            questions.append(l[2])
            if 'no' in l[3]:
                labels.append(0)
            elif "yes" in l[3]:
                labels.append(1)
        return torch.cat(images, dim=0), questions, torch.tensor(labels).to(torch.int64)



    test_loader = torch.utils.data.DataLoader(dev_datasets, batch_size=64, num_workers=8, shuffle=False,
                                              collate_fn=process_image_from_ofa)
    train_loader_cache = torch.utils.data.DataLoader(train_datasets, batch_size=256, num_workers=8, shuffle=False,
                                                     collate_fn=process_image_from_ofa)
    train_loader_F = torch.utils.data.DataLoader(train_datasets, batch_size=256, num_workers=8, shuffle=True,
                                                 collate_fn=process_image_from_ofa)

    # Construct the cache model by few-shot training set
    # print("\nConstructing cache model by few-shot visual features and labels.")
    cache_keys, cache_values = build_cache_model(cfg, clip_model, train_loader_cache)

    # Pre-load test features
    print("\nLoading visual features and labels from test set.")
    test_features, test_labels = pre_load_features(cfg, "test", clip_model, test_loader)

    # ------------------------------------------ Tip-Adapter-F ------------------------------------------
    run_tip_adapter_F(cfg, cache_keys, cache_values* 0.00001, test_features, test_labels, clip_model, train_loader_F)
           

if __name__ == '__main__':
    main()