accelerate launch train_ootd.py \
    --pretrained_model_name_or_path="/workspace/OOTDiffusion/checkpoints/ootd" \
    --mixed_precision="fp16" \
    --output_dir="/workspace/OOTDiffusion/output/logs/train_ootd" \
    --dataset_name="SaffalPoosh/VITON-HD-test" \
    --resolution="512" \
    --learning_rate="1e-5" \
    --train_batch_size="20" \
    --dataroot="/workspace/OOTDiffusion/data/VITON-HD" \
    --train_data_list="train_pairs.txt" \
    --validation_data_list="subtrain_20_bk.txt" \
    --test_data_list="subtest_20_bk.txt" \
    --num_train_epochs="150" \
    --checkpointing_steps="5000" \
    --use_8bit_adam \
    --gradient_checkpointing \
    --enable_xformers_memory_efficient_attention \
    --validation_steps="100" \
    --inference_steps="50" \
    --log_grads \
    --report_to="wandb" \
    --seed="42" \
    --clip_grad_norm \
    --gradient_accumulation_steps="4"  \
    --vton_unet_path="runwayml/stable-diffusion-v1-5"
    # --refactor_unet \
    # --tracker_project_name="train_OOTDdiffusion" \
    # --tracker_entity="xuziang" \