import os
import math
import wandb
import random
import logging
import inspect
import argparse
import datetime
import subprocess

from pathlib import Path
from tqdm.auto import tqdm
from einops import rearrange
from omegaconf import OmegaConf
from safetensors import safe_open
from typing import Dict, Optional, Tuple

import torch
import torchvision
import torch.nn.functional as F
import torch.distributed as dist
from torch.optim.swa_utils import AveragedModel
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP

import diffusers
from diffusers import AutoencoderKL, DDIMScheduler, UniPCMultistepScheduler
from diffusers.models import UNet2DConditionModel
# from diffusers.pipelines import StableDiffusionPipeline
from diffusers.optimization import get_scheduler
from diffusers.utils import check_min_version
from diffusers.utils.import_utils import is_xformers_available

import transformers
from transformers import CLIPTextModel, CLIPTokenizer

from pipelines_ootd.pipeline_ootd import OotdPipeline
from pipelines_ootd.unet_garm_2d_condition import UNetGarm2DConditionModel
from pipelines_ootd.unet_vton_2d_condition import UNetVton2DConditionModel

from data.dataset import CPDataset, collate_fn

from utils.util import  zero_rank_print
from models.ReferenceEncoder import ReferenceEncoder


import pdb

def init_dist(launcher="slurm", backend='nccl', port=28888, **kwargs):
    """Initializes distributed environment."""
    if launcher == 'pytorch':
        rank = int(os.environ['RANK'])
        num_gpus = torch.cuda.device_count()
        local_rank = rank % num_gpus
        torch.cuda.set_device(local_rank)
        dist.init_process_group(backend=backend, **kwargs)
        
    elif launcher == 'slurm':
        proc_id = int(os.environ['SLURM_PROCID'])
        ntasks = int(os.environ['SLURM_NTASKS'])
        node_list = os.environ['SLURM_NODELIST']
        num_gpus = torch.cuda.device_count()
        local_rank = proc_id % num_gpus
        torch.cuda.set_device(local_rank)
        addr = subprocess.getoutput(
            f'scontrol show hostname {node_list} | head -n1')
        os.environ['MASTER_ADDR'] = addr
        os.environ['WORLD_SIZE'] = str(ntasks)
        os.environ['RANK'] = str(proc_id)
        port = os.environ.get('PORT', port)
        os.environ['MASTER_PORT'] = str(port)
        dist.init_process_group(backend=backend)
        zero_rank_print(f"proc_id: {proc_id}; local_rank: {local_rank}; ntasks: {ntasks}; node_list: {node_list}; num_gpus: {num_gpus}; addr: {addr}; port: {port}")
        
    else:
        raise NotImplementedError(f'Not implemented launcher type: `{launcher}`!')
    
    return local_rank


def get_parameters_without_gradients(model):
    """
    Returns a list of names of the model parameters that have no gradients.

    Args:
    model (torch.nn.Module): The model to check.
    
    Returns:
    List[str]: A list of parameter names without gradients.
    """
    no_grad_params = []
    for name, param in model.named_parameters():
        print(f"{name} : {param.grad}")
        if param.grad is None:
            no_grad_params.append(name)
    return no_grad_params

UNET_PATH = "../checkpoints/ootd/ootd_hd/checkpoint-36000"
MODEL_PATH = "../checkpoints/ootd"

def main(
    image_finetune: bool,
    
    name: str,
    use_wandb: bool,
    launcher: str,
    
    output_dir: str,
    pretrained_model_path: str,
    clip_model_path:str,
    description: str,
    fusion_blocks: str,
    
    unet_garm_checkpoint_path: str,
    unet_vton_checkpoint_path: str,
    
    train_data: Dict,
    validation_data: Dict,
    cfg_random_null_text: bool = True,
    cfg_random_null_text_ratio: float = 0.1,
    
    unet_checkpoint_path: str = "",
    unet_additional_kwargs: Dict = {},
    ema_decay: float = 0.9999,
    noise_scheduler_kwargs = None,
    
    
    max_train_epoch: int = -1,
    max_train_steps: int = 100,
    validation_steps: int = 100,
    validation_steps_tuple: Tuple = (-1,),

    learning_rate: float = 3e-5,
    scale_lr: bool = False,
    lr_warmup_steps: int = 0,
    lr_scheduler: str = "constant",

    trainable_modules: Tuple[str] = (None, ),
    num_workers: int = 8,
    train_batch_size: int = 1,
    adam_beta1: float = 0.9,
    adam_beta2: float = 0.999,
    adam_weight_decay: float = 1e-2,
    adam_epsilon: float = 1e-08,
    max_grad_norm: float = 1.0,
    gradient_accumulation_steps: int = 1,
    gradient_checkpointing: bool = False,
    checkpointing_epochs: int = 5,
    checkpointing_steps: int = -1,

    mixed_precision_training: bool = True,
    enable_xformers_memory_efficient_attention: bool = True,

    global_seed: int = 42,
    is_debug: bool = False,
):
    check_min_version("0.21.4")

    # Initialize distributed training
    local_rank      = init_dist(launcher=launcher, port=28888)
    global_rank     = dist.get_rank()
    num_processes   = dist.get_world_size()
    # num_processes   = 0
    is_main_process = global_rank == 0

    seed = global_seed + global_rank
    torch.manual_seed(seed)
    
    # Logging folder
    folder_name = "debug" if is_debug else name + datetime.datetime.now().strftime("-%Y-%m-%dT%H-%M-%S")
    output_dir = os.path.join(output_dir, folder_name)
    if is_debug and os.path.exists(output_dir):
        os.system(f"rm -rf {output_dir}")

    *_, config = inspect.getargvalues(inspect.currentframe())

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )

    if is_main_process and (not is_debug) and use_wandb:
        run = wandb.init(project="CatchOn diffuser", name=folder_name, config=config)

    # Handle the output folder creation
    if is_main_process:
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(f"{output_dir}/samples", exist_ok=True)
        os.makedirs(f"{output_dir}/sanity_check", exist_ok=True)
        os.makedirs(f"{output_dir}/checkpoints", exist_ok=True)
        OmegaConf.save(config, os.path.join(output_dir, 'config.yaml'))
        
        print(description)

    # Load scheduler, tokenizer and models.
    noise_scheduler = UniPCMultistepScheduler(**OmegaConf.to_container(noise_scheduler_kwargs))

    vae          = AutoencoderKL.from_pretrained(pretrained_model_path, subfolder="vae")
    clip_image_encoder = ReferenceEncoder(model_path=clip_model_path)

    unet_garm = UNetGarm2DConditionModel.from_pretrained(
        UNET_PATH,
        subfolder="unet_garm",
        torch_dtype=torch.float16,
        use_safetensors=True,
    )

    unet_vton = UNetVton2DConditionModel.from_pretrained(
        UNET_PATH,
        subfolder="unet_vton",
        torch_dtype=torch.float16,
        use_safetensors=True,
    )    
    
    # Load pretrained unet weights
    
    if unet_garm_checkpoint_path != "":
        zero_rank_print(f"from checkpoint: {unet_garm_checkpoint_path}")
        unet_garm_checkpoint_path = torch.load(unet_garm_checkpoint_path, map_location="cpu")
        if "global_step" in unet_garm_checkpoint_path: zero_rank_print(f"global_step: {unet_garm_checkpoint_path['global_step']}")
        state_dict = unet_garm_checkpoint_path["state_dict"] if "state_dict" in unet_garm_checkpoint_path else unet_garm_checkpoint_path

        m, u = unet_garm.load_state_dict(state_dict, strict=False)
        zero_rank_print(f"missing keys: {len(m)}, unexpected keys: {len(u)}")
        assert len(u) == 0

    if unet_vton_checkpoint_path != "":
        zero_rank_print(f"from checkpoint: {unet_vton_checkpoint_path}")
        unet_vton_checkpoint_path = torch.load(unet_vton_checkpoint_path, map_location="cpu")
        if "global_step" in unet_vton_checkpoint_path: zero_rank_print(f"global_step: {unet_vton_checkpoint_path['global_step']}")
        state_dict = unet_vton_checkpoint_path["state_dict"] if "state_dict" in unet_vton_checkpoint_path else unet_vton_checkpoint_path

        m, u = unet_vton.load_state_dict(state_dict, strict=False)
        zero_rank_print(f"missing keys: {len(m)}, unexpected keys: {len(u)}")
        assert len(u) == 0

    # Freeze vae and text_encoder
    vae.requires_grad_(False)
    # text_encoder.requires_grad_(False)
    clip_image_encoder.requires_grad_(False)
    
    # Set unet trainable parameters

    unet_garm.requires_grad_(True)
    unet_vton.requires_grad_(True)
    # unet.requires_grad_(True)
    # TODO: An set up a detailed control on trainable modules
    # for name, param in unet.named_parameters():
    #     for trainable_module_name in trainable_modules:
    #         if trainable_module_name in name:
    #             # print(trainable_module_name)
    #             param.requires_grad = True
    #             break        
    
    trainable_params = list(filter(lambda p: p.requires_grad, unet_vton.parameters()))
    
    # print(len(trainable_params))
    # exit(0)
    
    optimizer = torch.optim.AdamW(
        trainable_params,
        lr=learning_rate,
        betas=(adam_beta1, adam_beta2),
        weight_decay=adam_weight_decay,
        eps=adam_epsilon,
    )

    if is_main_process:
        zero_rank_print(f"trainable params number: {len(trainable_params)}")
        zero_rank_print(f"trainable params scale: {sum(p.numel() for p in trainable_params) / 1e6:.3f} M")

    # Enable xformers
    if enable_xformers_memory_efficient_attention:
        if is_xformers_available():
            unet_vton.enable_xformers_memory_efficient_attention()
            unet_garm.enable_xformers_memory_efficient_attention()
            
        else:
            raise ValueError("xformers is not available. Make sure it is installed correctly")

    # Enable gradient checkpointing
    if gradient_checkpointing:
        unet_garm.enable_gradient_checkpointing()
        unet_vton.enable_gradient_checkpointing()
        

    # Move models to GPU
    vae.to(local_rank)
    # text_encoder.to(local_rank)
    clip_image_encoder.to(local_rank)


    # Get the training dataset
    # train_dataset = WebVid10M(**train_data, is_image=image_finetune)
    # train_dataset = TikTok(**train_data, is_image=image_finetune)
    # TODO: AN a new dataset loader is required here
    train_dataset = CPDataset(**train_data, is_image=image_finetune)
    
    distributed_sampler = DistributedSampler(
        train_dataset,
        num_replicas=num_processes,
        rank=global_rank,
        shuffle=True,
        seed=global_seed,
    )

    # DataLoaders creation:
    # TODO: AN a new dataset loader is required here
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=train_batch_size,
        shuffle=False,
        sampler=distributed_sampler,
        num_workers=num_workers,
        pin_memory=True,
        drop_last=True,
        collate_fn=collate_fn,
    )

    # Get the training iteration
    if max_train_steps == -1:
        assert max_train_epoch != -1
        max_train_steps = max_train_epoch * len(train_dataloader)
        
    if checkpointing_steps == -1:
        assert checkpointing_epochs != -1
        checkpointing_steps = checkpointing_epochs * len(train_dataloader)

    if scale_lr:
        learning_rate = (learning_rate * gradient_accumulation_steps * train_batch_size * num_processes)

    # Scheduler
    lr_scheduler = get_scheduler(
        lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=lr_warmup_steps * gradient_accumulation_steps,
        num_training_steps=max_train_steps * gradient_accumulation_steps,
    )

    # # Validation pipeline
    # TODO: An enable validation
    # if not image_finetune:
    #     validation_pipeline = AnimationPipeline(
    #         unet=unet, vae=vae, tokenizer=tokenizer, text_encoder=text_encoder, scheduler=noise_scheduler,
    #     ).to("cuda")
    # else:
    #     validation_pipeline = StableDiffusionPipeline.from_pretrained(
    #         pretrained_model_path,
    #         unet=unet, vae=vae, tokenizer=tokenizer, text_encoder=text_encoder, scheduler=noise_scheduler, safety_checker=None,
    #     )
    # validation_pipeline.enable_vae_slicing()

    # DDP warpper
    # To GPU
    unet_garm.to(local_rank)
    unet_vton.to(local_rank)
    unet_garm = DDP(unet_garm, device_ids=[local_rank], output_device=local_rank)
    unet_vton = DDP(unet_vton, device_ids=[local_rank], output_device=local_rank)
    

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / gradient_accumulation_steps)
    # Afterwards we recalculate our number of training epochs
    num_train_epochs = math.ceil(max_train_steps / num_update_steps_per_epoch)

    # Train!
    total_batch_size = train_batch_size * num_processes * gradient_accumulation_steps

    if is_main_process:
        logging.info("***** Running training *****")
        logging.info(f"  Num examples = {len(train_dataset)}")
        logging.info(f"  Num Epochs = {num_train_epochs}")
        logging.info(f"  Instantaneous batch size per device = {train_batch_size}")
        logging.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
        logging.info(f"  Gradient Accumulation steps = {gradient_accumulation_steps}")
        logging.info(f"  Total optimization steps = {max_train_steps}")
    global_step = 0
    first_epoch = 0

    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(global_step, max_train_steps), disable=not is_main_process)
    progress_bar.set_description("Steps")

    # Support mixed-precision training
    scaler = torch.cuda.amp.GradScaler() if mixed_precision_training else None

    for epoch in range(first_epoch, num_train_epochs):
        train_dataloader.sampler.set_epoch(epoch)
        unet_vton.train()
        unet_garm.train()
        
        
        for step, batch in enumerate(train_dataloader):
            # ToDo: add cfg_random_null_image to strength cfg
            # if cfg_random_null_text:
            #     batch['text'] = [name if random.random() > cfg_random_null_text_ratio else "" for name in batch['text']]
                
            # # Data batch sanity check
            # if epoch == first_epoch and step == 0:
            #     pixel_values, texts = batch['pixel_values'].cpu(), batch['text']
            #     if not image_finetune:
            #         pixel_values = rearrange(pixel_values, "b f c h w -> b c f h w")
            #         for idx, (pixel_value, text) in enumerate(zip(pixel_values, texts)):
            #             pixel_value = pixel_value[None, ...]
            #             save_videos_grid(pixel_value, f"{output_dir}/sanity_check/{'-'.join(text.replace('/', '').split()[:10]) if not text == '' else f'{global_rank}-{idx}'}.gif", rescale=True)
            #     else:
            #         for idx, (pixel_value, text) in enumerate(zip(pixel_values, texts)):
            #             pixel_value = pixel_value / 2. + 0.5
            #             torchvision.utils.save_image(pixel_value, f"{output_dir}/sanity_check/{'-'.join(text.replace('/', '').split()[:10]) if not text == '' else f'{global_rank}-{idx}'}.png")
                    
            ### >>>> Training >>>> ###
            
            # Convert videos to latent space            
            # TODO: AN Convert dataloader data to latent
            pixel_values = batch["pixel_values"].to(local_rank)
            clip_ref_image = batch["clip_ref_image"].to(local_rank)
            pixel_values_ref_img = batch["pixel_values_ref_img"].to(local_rank)
            drop_image_embeds = batch["drop_image_embeds"].to(local_rank) # torch.Size([bs])
            
            with torch.no_grad():
                
                latents = vae.encode(pixel_values).latent_dist
                latents = latents.sample()
                latents = latents * 0.18215
                
                latents_ref_img = vae.encode(pixel_values_ref_img).latent_dist
                latents_ref_img = latents_ref_img.sample()
                latents_ref_img = latents_ref_img * 0.18215

            # Sample noise that we'll add to the latents
            noise = torch.randn_like(latents)
            bsz = latents.shape[0]
            
            # Sample a random timestep for each video
            timesteps = torch.randint(0, noise_scheduler.config.num_train_timesteps, (bsz,), device=latents.device)
            timesteps = timesteps.long()
            
            # Add noise to the latents according to the noise magnitude at each timestep
            noisy_latents = noise_scheduler.add_noise(latents, noise, timesteps)
            
            
            # if not image_finetune:
            #     pixel_values_pose = rearrange(pixel_values_pose, "b f c h w -> (b f) c h w")
            #     latents_pose = poseguider(pixel_values_pose)
            #     latents_pose = rearrange(latents_pose, "(b f) c h w -> b c f h w", f=video_length)
            # else:
            #     latents_pose = poseguider(pixel_values_pose)
            
            noisy_latents = noisy_latents
            
            # Get the text embedding for conditioning
            with torch.no_grad():
                # prompt_ids = tokenizer(
                #     batch['text'], max_length=tokenizer.model_max_length, padding="max_length", truncation=True, return_tensors="pt"
                # ).input_ids.to(latents.device)
                # encoder_hidden_states = text_encoder(prompt_ids)[0]
                encoder_hidden_states = clip_image_encoder(clip_ref_image).unsqueeze(1) # [bs,1,768]
            
            # support cfg train
            mask = drop_image_embeds > 0
            mask = mask.unsqueeze(1).unsqueeze(2).expand_as(encoder_hidden_states)
            encoder_hidden_states[mask] = 0

            # pdb.set_trace()
            
            # Get the target for loss depending on the prediction type
            if noise_scheduler.config.prediction_type == "epsilon":
                target = noise
            elif noise_scheduler.config.prediction_type == "v_prediction":
                raise NotImplementedError
            else:
                raise ValueError(f"Unknown prediction type {noise_scheduler.config.prediction_type}")

            # Predict the noise residual and compute loss
            # Mixed-precision training
            with torch.cuda.amp.autocast(enabled=mixed_precision_training):
                ref_timesteps = torch.zeros_like(timesteps)
                
                # pdb.set_trace()
                # TODO: AN review this refnet 
                # referencenet(latents_ref_img, ref_timesteps, encoder_hidden_states)
                # reference_control_reader.update(reference_control_writer)
                
                model_pred = unet_vton(noisy_latents, timesteps, encoder_hidden_states).sample
                loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")
                
                
            optimizer.zero_grad()

            # Backpropagate
            if mixed_precision_training:
                scaler.scale(loss).backward()
                """ >>> gradient clipping >>> """
                scaler.unscale_(optimizer)
                # torch.nn.utils.clip_grad_norm_(unet.parameters(), max_grad_norm)
                torch.nn.utils.clip_grad_norm_(trainable_params, max_grad_norm)
                """ <<< gradient clipping <<< """
                scaler.step(optimizer)
                scaler.update()
            else:
                loss.backward()
                
                # pdb.set_trace()
                
                # no_grad_params_poseguider = get_parameters_without_gradients(poseguider)
                # no_grad_params_referencenet = get_parameters_without_gradients(referencenet)
                # if len(no_grad_params_poseguider) != 0:
                #     print("PoseGuider no grad params:", no_grad_params_poseguider)
                # if len(no_grad_params_referencenet) != 0:
                #     print("ReferenceNet no grad params:", no_grad_params_referencenet)
                
                """ >>> gradient clipping >>> """
                # torch.nn.utils.clip_grad_norm_(unet.parameters(), max_grad_norm)
                torch.nn.utils.clip_grad_norm_(trainable_params, max_grad_norm)
                """ <<< gradient clipping <<< """
                optimizer.step()

            lr_scheduler.step()
            progress_bar.update(1)
            
            # TODO: AN review this refnet 
            # reference_control_reader.clear()
            # reference_control_writer.clear()
            global_step += 1
            
            ### <<<< Training <<<< ###
            
            # Wandb logging
            if is_main_process and (not is_debug) and use_wandb:
                wandb.log({"train_loss": loss.item()}, step=global_step)
                
            # Save checkpoint
            # if is_main_process and (global_step % checkpointing_steps == 0 or step == len(train_dataloader) - 1):
            if is_main_process and global_step % checkpointing_steps == 0 :
                save_path = os.path.join(output_dir, f"checkpoints")
                state_dict = {
                    "epoch": epoch,
                    "global_step": global_step,
                    "unet_garm_state_dict": unet_garm.module.state_dict(),
                    "unet_vton_state_dict": unet_vton.module.state_dict(),                    
                }
                if step == len(train_dataloader) - 1:
                    torch.save(state_dict, os.path.join(save_path, f"checkpoint-epoch-{epoch+1}.ckpt"))
                else:
                    torch.save(state_dict, os.path.join(save_path, f"checkpoint-global_step-{global_step}.ckpt"))
                logging.info(f"Saved state to {save_path} (global_step: {global_step})")
                
            # # Periodically validation
            # if is_main_process and (global_step % validation_steps == 0 or global_step in validation_steps_tuple):
            #     samples = []
                
            #     generator = torch.Generator(device=latents.device)
            #     generator.manual_seed(global_seed)
                
            #     height = train_data.sample_size[0] if not isinstance(train_data.sample_size, int) else train_data.sample_size
            #     width  = train_data.sample_size[1] if not isinstance(train_data.sample_size, int) else train_data.sample_size

            #     prompts = validation_data.prompts[:2] if global_step < 1000 and (not image_finetune) else validation_data.prompts

            #     for idx, prompt in enumerate(prompts):
            #         if not image_finetune:
            #             sample = validation_pipeline(
            #                 prompt,
            #                 generator    = generator,
            #                 video_length = train_data.sample_n_frames,
            #                 height       = height,
            #                 width        = width,
            #                 **validation_data,
            #             ).videos
            #             save_videos_grid(sample, f"{output_dir}/samples/sample-{global_step}/{idx}.gif")
            #             samples.append(sample)
                        
            #         else:
            #             sample = validation_pipeline(
            #                 prompt,
            #                 generator           = generator,
            #                 height              = height,
            #                 width               = width,
            #                 num_inference_steps = validation_data.get("num_inference_steps", 25),
            #                 guidance_scale      = validation_data.get("guidance_scale", 8.),
            #             ).images[0]
            #             sample = torchvision.transforms.functional.to_tensor(sample)
            #             samples.append(sample)
                
                # if not image_finetune:
                #     samples = torch.concat(samples)
                #     save_path = f"{output_dir}/samples/sample-{global_step}.gif"
                #     save_videos_grid(samples, save_path)
                    
                # else:
                #     samples = torch.stack(samples)
                #     save_path = f"{output_dir}/samples/sample-{global_step}.png"
                #     torchvision.utils.save_image(samples, save_path, nrow=4)

                # logging.info(f"Saved samples to {save_path}")
                
            logs = {"step_loss": loss.detach().item(), "lr": lr_scheduler.get_last_lr()[0]}
            progress_bar.set_postfix(**logs)
            
            if global_step >= max_train_steps:
                break
            
    dist.destroy_process_group()



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",   type=str, required=True)
    parser.add_argument("--launcher", type=str, choices=["pytorch", "slurm"], default="pytorch")
    parser.add_argument("--wandb",    action="store_true")
    args = parser.parse_args()

    name   = Path(args.config).stem
    config = OmegaConf.load(args.config)

    main(name=name, launcher=args.launcher, use_wandb=args.wandb, **config)
    

    # CUDA_VISIBLE_DEVICES=1 torchrun --nnodes=1 --nproc_per_node=1 train.py --config configs/training/train_stage_1_oneshot.yaml
    # CUDA_VISIBLE_DEVICES=2,3 torchrun --nnodes=1 --nproc_per_node=2 --master_port 28888 train.py --config configs/training/train_stage_1.yaml
    # CUDA_VISIBLE_DEVICES=2,3,4,5,6,7 torchrun --nnodes=1 --nproc_per_node=6 --master_port 28889 train.py --config configs/training/train_stage_1.yaml
    # CUDA_VISIBLE_DEVICES=4,5,6,7 torchrun --nnodes=1 --nproc_per_node=4 --master_port 28887 train.py --config configs/training/train_stage_1.yaml

    # CUDA_VISIBLE_DEVICES=7 torchrun --nnodes=1 --nproc_per_node=1 train.py --config configs/training/train_stage_2.yaml