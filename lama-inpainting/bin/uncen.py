from argparse import ArgumentParser
import os
import sys
import traceback

from saicinpainting.evaluation.utils import move_to_device
from saicinpainting.evaluation.data import ceil_modulo
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
os.environ['VECLIB_MAXIMUM_THREADS'] = '1'
os.environ['NUMEXPR_NUM_THREADS'] = '1'

import cv2
import hydra
import numpy as np
import torch
import tqdm
import yaml
from omegaconf import OmegaConf
from torch.utils.data._utils.collate import default_collate

from saicinpainting.training.data.datasets import make_default_val_dataset
from saicinpainting.training.data.datasets import make_default_train_dataloader
from saicinpainting.training.trainers import load_checkpoint
from saicinpainting.utils import register_debug_signal_handlers


from utils import find_regions
import torch.nn.functional as F

from saicinpainting.training.data.masks import get_mask_generator


def pad_crop_context(tensor, pad):
    """Pad crop context without violating PyTorch reflection constraints.

    ReflectionPad requires every pad amount to be smaller than the source
    dimension.  Comic pages can be much shorter or narrower than the mask-
    derived crop radius, so preserve reflection independently on dimensions
    where it is valid and use edge replication only on the constrained axis.
    """
    if pad < 0:
        raise ValueError("Padding must be non-negative")
    if pad == 0:
        return tensor

    height, width = tensor.shape[-2:]
    horizontal_mode = 'reflect' if width > 1 and pad < width else 'replicate'
    padded = F.pad(tensor, (pad, pad, 0, 0), mode=horizontal_mode)
    vertical_mode = 'reflect' if height > 1 and pad < height else 'replicate'
    return F.pad(padded, (0, 0, pad, pad), mode=vertical_mode)



def init_inpaint_model(model_path):

    train_config_path = os.path.join(model_path, 'config.yaml')
    with open(train_config_path, 'r') as f:
        train_config = OmegaConf.create(yaml.safe_load(f))

    train_config.training_model.predict_only = True
    train_config.visualizer.kind = 'noop'

    checkpoint_path = os.path.join(model_path, 
                                   'models', 
                                   'best.ckpt')
    model = load_checkpoint(train_config, checkpoint_path, strict=False, map_location='cpu')
    model.freeze()

    return model


def inpaint(model, image_orig, mask_orig):

    ker = np.ones((0,0), dtype=np.uint8)
    mask_orig = cv2.dilate(mask_orig[..., 0], kernel=ker, iterations=1)[..., None]

    use_mask_blend = True
    mask_blend = cv2.dilate(mask_orig[..., 0], kernel=ker, iterations=4)[..., None]
    if not use_mask_blend:
        mask_blend.fill(255)

    def proc(image_orig_p):
        image_p = image_orig_p.copy()
        if image_p.ndim == 3:
            image_p = np.transpose(image_p, (2, 0, 1))
        image_p = image_p.astype('float32') / 255
        return image_p

    image = proc(image_orig)
    mask  = proc(mask_orig)

    f = 4
    ker = np.ones((0,0), dtype=np.uint8)
    mask_orig2 = cv2.resize(255-mask_orig, (mask_orig.shape[1]//f, mask_orig.shape[0]//f), cv2.INTER_AREA)
    its = 36//f
    mask_orig2 = cv2.erode(mask_orig2, kernel=ker, iterations=its)
    regions = find_regions(np.dstack([mask_orig2, mask_orig2, mask_orig2]), (0, 0, 0)) 
    for i in range(len(regions)):
        regions[i] = np.array(list(regions[i])) * f


    out = image_orig.copy()
    out_orig = image_orig.copy()
    for pixel_set in regions:
        s = np.array(list(pixel_set))
        min_x = np.min(s[:, 1])
        min_y = np.min(s[:, 0])
        max_x = np.max(s[:, 1])
        max_y = np.max(s[:, 0])
        c_y, c_x = (max_y + min_y)//2, (max_x + min_x)//2
        r_h, r_w = max_y - min_y, max_x - min_x
        
        pix_cnt = (mask_orig[min_y:max_y, min_x:max_x, 0]/255).sum() 
        density = pix_cnt / (r_h * r_w)
        #print('HERE', pix_cnt, density)
        if r_h < 10 and r_w < 10 or pix_cnt < 100:
            continue

        const_pp = False
        if const_pp:
            pp = 256
        else:
            fac = 0.6
            pp = int(round(max(r_h, r_w)*fac))
            pp = ceil_modulo(pp, 8)
        #print(len(regions), pp)
        rsy, rey, rsx, rex = c_y-pp, c_y+pp, c_x-pp, c_x+pp
        #h, w = rey - rsy, rex - rsx
        pad = pp + 1

        region = pad_crop_context(
            torch.from_numpy(image).unsqueeze(0), pad
        )[:, :, pad+rsy:pad+rey, pad+rsx:pad+rex].numpy()[0]


        region_mask = pad_crop_context(
            torch.from_numpy(mask).unsqueeze(0), pad
        )[:, :, pad+rsy:pad+rey, pad+rsx:pad+rex].numpy()[0]

        batch_o = [dict(
                image=region,
                mask=region_mask,
                unpad_to_size=(pp*2,pp*2),
        )]
        batch = default_collate(batch_o)

        with torch.no_grad():
            batch = move_to_device(batch, model.device)
            batch['mask'] = (batch['mask'] > 0) * 1
            batch = model(batch)                    
            key = 'inpainted'
            cur_res = batch[key][0].permute(1, 2, 0).detach().cpu().numpy()
            unpad_to_size = batch.get('unpad_to_size', None)
            if unpad_to_size is not None:
                orig_height, orig_width = unpad_to_size
                cur_res = cur_res[:orig_height, :orig_width]

        cur_mask = mask_blend[max(0,rsy):rey, max(0,rsx):rex, :] / 255
        out[max(0,rsy):rey, max(0,rsx):rex, :] = (cur_res[max(0, -rsy):min(pp*2, pp*2-(rey-out.shape[0])), max(0, -rsx):min(pp*2, pp*2-(rex-out.shape[1])), :] * 255) * cur_mask    +   out[max(0,rsy):rey, max(0,rsx):rex, :] * (1-cur_mask) 
        cv2.rectangle(out_orig, (max(0,rsx),max(0,rsy)), (rex,rey), color=(0,0,255), thickness=2)

    out_orig[..., 0][mask_orig[..., 0] > 0.1] //= 2
    out_orig[..., 1][mask_orig[..., 0] > 0.1] += ((255 - out_orig[..., 1][mask_orig[..., 0] > 0.1])*0.25).astype(out_orig.dtype)
    out_orig[..., 2][mask_orig[..., 0] > 0.1] //= 2
    out_dbg = np.concatenate([out_orig, out], axis=1)

    return out, out_dbg


def process_directory_recursively(input_dir, mask_dir, output_dir, debug_dir, model):
    workspace_root = os.environ.get('WORKSPACE_ROOT', '')
    input_files = []
    for root, _, files in os.walk(input_dir):
        input_files.extend(
            os.path.join(root, file)
            for file in sorted(files)
            if file.lower().endswith('.png')
        )
    print(f"Inpainting found {len(input_files)} image(s) in {input_dir}", flush=True)
    failures = []

    for index, in_file in enumerate(input_files, start=1):
        root = os.path.dirname(in_file)
        file = os.path.basename(in_file)
        relative_path = os.path.relpath(root, input_dir)
        output_subdir = os.path.join(output_dir, relative_path)
        debug_subdir = os.path.join(debug_dir, relative_path) if debug_dir else None

        os.makedirs(output_subdir, exist_ok=True)
        if debug_subdir:
            os.makedirs(debug_subdir, exist_ok=True)

        mask_file = os.path.join(mask_dir, relative_path, file)

        if not os.path.exists(mask_file):
            base_name = os.path.splitext(file)[0]
            mask_file = next(
                (
                    candidate
                    for ext in ['.png', '.jpg', '.jpeg', '.webp']
                    for candidate in [os.path.join(mask_dir, relative_path, base_name + ext)]
                    if os.path.exists(candidate)
                ),
                None,
            )
            if mask_file is None:
                failures.append((in_file, 'matching mask not found'))
                print(f"Inpainting failed: {in_file} (matching mask not found)", flush=True)
                continue

        display_path = in_file
        if workspace_root and in_file.startswith(workspace_root):
            display_path = in_file[len(workspace_root):].lstrip(os.sep)
        if index == 1 or index == len(input_files) or index % 25 == 0:
            print(
                f"Inpainting progress {index}/{len(input_files)}: {display_path}",
                flush=True,
            )

        try:
            raw_image = cv2.imread(in_file)
            mask = cv2.imread(mask_file)
            if raw_image is None:
                raise ValueError('image could not be read')
            if mask is None:
                raise ValueError(f'mask could not be read: {mask_file}')
            img = cv2.cvtColor(raw_image, cv2.COLOR_BGR2RGB)

            output, dbg = inpaint(model, img, mask)

            out_path = os.path.join(output_subdir, file)
            if not cv2.imwrite(out_path, cv2.cvtColor(output, cv2.COLOR_BGR2RGB)):
                raise OSError(f'could not write output: {out_path}')

            if debug_subdir:
                dbg_path = os.path.join(debug_subdir, file)
                if not cv2.imwrite(dbg_path, cv2.cvtColor(dbg, cv2.COLOR_BGR2RGB)):
                    raise OSError(f'could not write debug output: {dbg_path}')
        except Exception as exc:
            failures.append((in_file, f'{type(exc).__name__}: {exc}'))
            print(
                f"Inpainting failed: {display_path} ({type(exc).__name__}: {exc})",
                flush=True,
            )

    if failures:
        failed_paths = ', '.join(path for path, _ in failures[:5])
        raise RuntimeError(
            f"Inpainting failed for {len(failures)} of {len(input_files)} image(s): "
            f"{failed_paths}"
        )
    return len(input_files)


def main():
    parser = ArgumentParser()
    parser.add_argument('--in_dir', required=True, help='dir with input images')
    parser.add_argument('--mask_dir', required=True, help='dir with input masks')
    parser.add_argument('--out_dir', required=True, help='dir with inpainted outputs')
    parser.add_argument('--checkpoint', required=True, help='Checkpoint dir')
    parser.add_argument('--device', default='cuda:0', help='Device used for inference')
    parser.add_argument('--debug_dir', default=None, help='dir with debug output')
    args = parser.parse_args()

    model = init_inpaint_model(args.checkpoint)
    device = torch.device(args.device)
    model.to(device)

    if not os.path.exists(args.out_dir):
        os.makedirs(args.out_dir)
    if args.debug_dir and not os.path.exists(args.debug_dir):
        os.makedirs(args.debug_dir)

    processed_count = process_directory_recursively(
        args.in_dir, args.mask_dir, args.out_dir, args.debug_dir, model
    )
    print(f"Inpainting completed for {processed_count} image(s)", flush=True)


if __name__ == '__main__':
    main()
