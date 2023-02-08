from model.sync_batchnorm import convert_model
from model import network
import datasets_ws
import commons
import parser
import test
import util
import math
import torch
import logging
import numpy as np
from tqdm import tqdm
import torch.nn as nn
import multiprocessing
from os.path import join
from datetime import datetime
import torchvision.transforms as transforms
from torch.utils.data.dataloader import DataLoader
import copy
from model.msssim import ssim
import wandb
torch.backends.cudnn.benchmark = True  # Provides a speedup

NOTABLE_IMAGES = [880, 881, 882, 883, 884, 885, 886, 887, 888, 889]

# Initial setup: parser, logging...
args = parser.parse_arguments()
start_time = datetime.now()
args.save_dir = join(
    "logs",
    args.save_dir,
    f"{args.dataset_name}-{start_time.strftime('%Y-%m-%d_%H-%M-%S')}",
)
commons.setup_logging(args.save_dir)
commons.make_deterministic(args.seed)
logging.info(f"Arguments: {args}")
wandb.init(project="VTLG", entity="xjh19971", config=vars(args))
logging.info(f"The outputs are being saved in {args.save_dir}")
logging.info(
    f"Using {torch.cuda.device_count()} GPUs and {multiprocessing.cpu_count()} CPUs"
)

# Creation of Datasets
logging.debug(
    f"Loading dataset {args.dataset_name} from folder {args.datasets_folder}")

train_ds = None
train_ds = datasets_ws.TranslationDataset(
    args, args.datasets_folder, args.dataset_name, "train", clean_black_region=True)

logging.info(f"Train query set: {train_ds}")

val_ds = datasets_ws.TranslationDataset(
    args, args.datasets_folder, args.dataset_name, "val", clean_black_region=False)
logging.info(f"Val set: {val_ds}")

test_ds = datasets_ws.TranslationDataset(
    args, args.datasets_folder, args.dataset_name, "test", clean_black_region=False)
logging.info(f"Test set: {test_ds}")

# Initialize model
if args.G_gray:
    model = network.pix2pix(args, 3, 1, for_training=True)
else:   
    model = network.pix2pix(args, 3, 3, for_training=True)

model.setup()

# Resume model, optimizer, and other training parameters
if args.resume:
    (
        model,
        _,
        best_psnr,
        start_epoch_num,
        not_improved_num,
    ) = util.resume_train(args, model)
    logging.info(
        f"Resuming from epoch {start_epoch_num} with best PSNR {best_psnr:.1f}",
    )
else:
    best_psnr = start_epoch_num = not_improved_num = 0
best_msssim = 0

# Training loop
for epoch_num in range(start_epoch_num, args.epochs_num):
    logging.info(f"Start training epoch: {epoch_num:02d}")

    epoch_start_time = datetime.now()
    epoch_losses_GAN = np.zeros((0, 1), dtype=np.float32)
    epoch_losses_AUX = np.zeros((0, 1), dtype=np.float32)
    # How many loops should an epoch last (default is 5000/1000=5)
    loops_num = math.ceil(args.queries_per_epoch / args.cache_refresh_rate)
    for loop_num in range(loops_num):
        logging.debug(f"Cache: {loop_num} / {loops_num}")

        # Compute pairs to use in the pair loss
        train_ds.is_inference = True
        train_ds.compute_pairs(args)
        train_ds.is_inference = False

        pairs_dl = DataLoader(
            dataset=train_ds,
            num_workers=args.num_workers,
            batch_size=args.train_batch_size,
            pin_memory=(args.device == "cuda"),
            drop_last=True,
        )

        model.netG = model.netG.train()
        model.netD = model.netD.train()

        # images shape: (train_batch_size*12)*3*H*W ; by default train_batch_size=4, H=512, W=512
        # pairs_local_indexes shape: (train_batch_size*10)*3 ; because 10 pairs per query
        for query, database in tqdm(pairs_dl, ncols=100):
            # Compute features of all images (images contains queries, positives and negatives)
            model.set_input(database, query)
            model.optimize_parameters()
            loss_GAN = model.loss_G_GAN
            loss_AUX = model.loss_G_L1

            # Keep track of all losses by appending them to epoch_losses
            batch_loss_GAN = loss_GAN.item()
            epoch_losses_GAN = np.append(epoch_losses_GAN, batch_loss_GAN)
            batch_loss_AUX = loss_AUX.item()
            epoch_losses_AUX = np.append(epoch_losses_AUX, batch_loss_AUX)
        debug_str = f"Epoch[{epoch_num:02d}]({loop_num}/{loops_num}): "+ \
            f"current batch sum GAN loss = {batch_loss_GAN:.4f}, "+ \
            f"average epoch sum GAN loss = {epoch_losses_GAN.mean():.4f}, "+ \
            f"current batch sum AUX loss = {batch_loss_AUX:.4f}, "+ \
            f"average epoch sum AUX loss = {epoch_losses_AUX.mean():.4f}, "

        logging.debug(debug_str)
    
    info_str = f"Finished epoch {epoch_num:02d} in {str(datetime.now() - epoch_start_time)[:-7]}, "+ \
        f"average epoch sum GAN loss = {epoch_losses_GAN.mean():.4f}, "+ \
        f"average epoch sum AUX loss = {epoch_losses_AUX.mean():.4f}, "

    logging.info(info_str)

    if args.GAN_save_freq != 0 and epoch_num % args.GAN_save_freq == 0:
        visual_current = True
    else:
        visual_current = False
    # Compute rPSNR on validation set
    psnr, psnr_str = test.test_translation_pix2pix(args, val_ds, model, visual_current, notable_image = NOTABLE_IMAGES, epoch_num=epoch_num)
    logging.info(f"PSNR on val set {val_ds}: {psnr_str}")

    is_best_psnr = psnr[0] > best_psnr
    is_best_msssim = psnr[1] > best_msssim

    wandb.log({
            "epoch_num": epoch_num,
            "psnr": psnr[0],
            "best_psnr": psnr[0] if is_best_psnr else best_psnr,
            "msssim": psnr[1],
            "best_msssim": psnr[1] if is_best_msssim else best_msssim,
            "GAN_loss": epoch_losses_GAN.mean(),
            "AUX_loss": epoch_losses_AUX.mean(),
        },)

    # Save checkpoint, which contains all training parameters
    util.save_checkpoint(
        args,
        {
            "epoch_num": epoch_num,
            "model_netD_state_dict": model.netD.state_dict(),
            "model_netG_state_dict": model.netG.state_dict(),
            "optimizer_netD_state_dict": model.optimizer_D.state_dict(),
            "optimizer_netG_state_dict": model.optimizer_G.state_dict(),
            "psnr": psnr[0],
            "msssim": psnr[1],
            "best_psnr": best_psnr,
            "not_improved_num": not_improved_num,
        },
        is_best_psnr,
        filename="last_model.pth",
        suffix="_psnr",
    )
    util.save_checkpoint(
        args,
        {
            "epoch_num": epoch_num,
            "model_netD_state_dict": model.netD.state_dict(),
            "model_netG_state_dict": model.netG.state_dict(),
            "optimizer_netD_state_dict": model.optimizer_D.state_dict(),
            "optimizer_netG_state_dict": model.optimizer_G.state_dict(),
            "psnr": psnr[0],
            "msssim": psnr[1],
            "best_msssim": best_msssim,
            "not_improved_num": not_improved_num,
        },
        is_best_msssim,
        filename="last_model.pth",
        suffix="_msssim",
    )

    # If PSNR did not improve for "many" epochs, stop training
    if is_best_psnr:
        logging.info(
            f"Improved: previous best PSNR = {best_psnr:.1f}, current PSNR = {psnr[0]:.1f}"
        )
        best_psnr = psnr[0]
        not_improved_num = 0
    if is_best_msssim:
        logging.info(
            f"Improved: previous best MS-SSIM = {best_msssim:.1f}, current MS-SSIM = {psnr[1]:.1f}"
        )
        best_msssim = psnr[1]
        not_improved_num = 0
    if not (is_best_psnr or is_best_msssim):
        not_improved_num += 1
        logging.info(
            f"Not improved: {not_improved_num} / {args.patience}"
        )
        if not_improved_num >= args.patience:
            logging.info(
                f"Performance did not improve for {not_improved_num} epochs. Stop training."
            )
            break
        
    if args.GAN_save_freq != 0 and epoch_num % args.GAN_save_freq == 0:
        util.save_checkpoint(
        args,
        {
            "epoch_num": epoch_num,
            "model_netD_state_dict": model.netD.state_dict(),
            "model_netG_state_dict": model.netG.state_dict(),
            "optimizer_netD_state_dict": model.optimizer_D.state_dict(),
            "optimizer_netG_state_dict": model.optimizer_G.state_dict(),
            "psnr": psnr[0],
            "msssim": psnr[1],
            "not_improved_num": not_improved_num,
        },
        False,
        filename=f"last_model_{epoch_num}.pth"
    )

logging.info(f"Best PSNR: {best_psnr:.1f}, Best MS-SSIM: {best_msssim:.1f}")
logging.info(
    f"Trained for {epoch_num+1:02d} epochs, in total in {str(datetime.now() - start_time)[:-7]}"
)

# Test best model on test set
# best_model_state_dict = torch.load(join(args.save_dir, "best_model.pth"))[
#     "model_state_dict"
# ]
# model.load_state_dict(best_model_state_dict)

# psnr, psnr_str = test.test_translation_pix2pix(
#     args, test_ds, model)
        
# logging.info(f"PSNR on {test_ds}: {psnr_str}")
