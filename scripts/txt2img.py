import argparse, os, sys, glob
import cv2
import torch
import numpy as np
from omegaconf import OmegaConf
from PIL import Image
from tqdm import tqdm, trange
from imwatermark import WatermarkEncoder
from itertools import islice
from einops import rearrange
from torchvision.utils import make_grid
import time
from pytorch_lightning import seed_everything
from torch import autocast
from contextlib import contextmanager, nullcontext
from random import randint

from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler
from ldm.models.diffusion.dpm_solver import DPMSolverSampler

from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
from transformers import AutoFeatureExtractor


# load safety model
safety_model_id = "CompVis/stable-diffusion-safety-checker"
safety_feature_extractor = AutoFeatureExtractor.from_pretrained(safety_model_id)
safety_checker = StableDiffusionSafetyChecker.from_pretrained(safety_model_id)


def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


def numpy_to_pil(images):
    """
    Convert a numpy image or a batch of images to a PIL image.
    """
    if images.ndim == 3:
        images = images[None, ...]
    images = (images * 255).round().astype("uint8")
    pil_images = [Image.fromarray(image) for image in images]

    return pil_images


def load_model_from_config(config, ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)

    model.cuda()
    model.eval()
    return model


def put_watermark(watermark_protection, img, wm_encoder=None):
    if watermark_protection and wm_encoder is not None:
        img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        img = wm_encoder.encode(img, 'dwtDct')
        img = Image.fromarray(img[:, :, ::-1])
    return img


def load_replacement(x):
    try:
        hwc = x.shape
        y = Image.open("assets/rick.jpeg").convert("RGB").resize((hwc[1], hwc[0]))
        y = (np.array(y)/255.0).astype(x.dtype)
        assert y.shape == x.shape
        return y
    except Exception:
        return x


def check_safety(nsfw_filter_switch, x_image):
    if nsfw_filter_switch:
        safety_checker_input = safety_feature_extractor(numpy_to_pil(x_image), return_tensors="pt")
        x_checked_image, has_nsfw_concept = safety_checker(images=x_image, clip_input=safety_checker_input.pixel_values)
        assert x_checked_image.shape[0] == len(has_nsfw_concept)
        for i in range(len(has_nsfw_concept)):
            if has_nsfw_concept[i]:
                x_checked_image[i] = load_replacement(x_checked_image[i])
        return x_checked_image, has_nsfw_concept
    else:
        return x_image, False


def resize_image(source_image, destination_image, width, height, resize_factor):
    image_to_resize = cv2.imread(source_image)
    resized_image = cv2.resize(image_to_resize, dsize=(width*resize_factor, height*resize_factor)
                               , interpolation=cv2.INTER_LANCZOS4)
    cv2.imwrite(destination_image, resized_image)


def improve_image(source_image, destination_image):
    image_to_improve = cv2.imread(source_image)
    image_improved = cv2.detailEnhance(image_to_improve, sigma_s=200, sigma_r=0.01)
    image_improved = cv2.edgePreservingFilter(image_improved, flags=1, sigma_s=60, sigma_r=0.3)
    cv2.imwrite(destination_image, image_improved)

def read_prompt_parameter(parser):
    parser.add_argument(
        "--prompt",
        type=str,
        nargs="?",
        default="a painting of a virus monster playing guitar",
        help="the prompt to render"
    )


def read_outdir_parameter(parser):
    parser.add_argument(
        "--outdir",
        type=str,
        nargs="?",
        help="dir to write results to",
        default="outputs/txt2img-samples"
    )


def read_skip_grid_parameter(parser):
    parser.add_argument(
        "--skip_grid",
        action='store_true',
        help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",
    )


def read_skip_save_parameter(parser):
    parser.add_argument(
        "--skip_save",
        action='store_true',
        help="do not save individual samples. For speed measurements.",
    )


def read_ddim_steps_parameter(parser):
    parser.add_argument(
        "--ddim_steps",
        type=int,
        default=50,
        help="number of ddim sampling steps",
    )


def read_plms_parameter(parser):
    parser.add_argument(
        "--plms",
        action='store_true',
        help="use plms sampling",
    )


def read_dpm_solver_parameter(parser):
    parser.add_argument(
        "--dpm_solver",
        action='store_true',
        help="use dpm_solver sampling",
    )


def read_laion400m_parameter(parser):
    parser.add_argument(
        "--laion400m",
        action='store_true',
        help="uses the LAION400M model",
    )


def read_fixed_code_parameter(parser):
    parser.add_argument(
        "--fixed_code",
        action='store_true',
        help="if enabled, uses the same starting code across samples ",
    )


def read_ddim_eta_parameter(parser):
    parser.add_argument(
        "--ddim_eta",
        type=float,
        default=0.0,
        help="ddim eta (eta=0.0 corresponds to deterministic sampling",
    )


def read_n_iter_parameter(parser):
    parser.add_argument(
        "--n_iter",
        type=int,
        default=2,
        help="sample this often",
    )


def read_height_parameter(parser):
    parser.add_argument(
        "--H",
        type=int,
        default=512,
        help="image height, in pixel space",
    )


def read_width_parameter(parser):
    parser.add_argument(
        "--W",
        type=int,
        default=512,
        help="image width, in pixel space",
    )


def read_latent_channels_parameter(parser):
    parser.add_argument(
        "--C",
        type=int,
        default=4,
        help="latent channels",
    )


def read_downsampling_factor_parameter(parser):
    parser.add_argument(
        "--f",
        type=int,
        default=8,
        help="downsampling factor",
    )


def read_n_samples_parameter(parser):
    parser.add_argument(
        "--n_samples",
        type=int,
        default=3,
        help="how many samples to produce for each given prompt. A.k.a. batch size",
    )


def read_n_rows_parameter(parser):
    parser.add_argument(
        "--n_rows",
        type=int,
        default=0,
        help="rows in the grid (default: n_samples)",
    )


def read_scale_parameter(parser):
    parser.add_argument(
        "--scale",
        type=float,
        default=7.5,
        help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
    )


def read_from_file_parameter(parser):
    parser.add_argument(
        "--from-file",
        type=str,
        help="if specified, load prompts from this file",
    )


def read_config_parameter(parser):
    parser.add_argument(
        "--config",
        type=str,
        default="configs/stable-diffusion/v1-inference.yaml",
        help="path to config which constructs model",
    )


def read_ckpt_parameter(parser):
    parser.add_argument(
        "--ckpt",
        type=str,
        default="models/ldm/stable-diffusion-v1/model.ckpt",
        help="path to checkpoint of model",
    )


def read_seed_parameter(parser):
    parser.add_argument(
        "--seed",
        type=int,
        default=randint(1, 4294967295),
        help="the seed (for reproducible sampling)",
    )


def read_precision_parameter(parser):
    parser.add_argument(
        "--precision",
        type=str,
        help="evaluate at this precision",
        choices=["full", "autocast"],
        default="autocast"
    )


def read_nsfw_protection_parameter(parser):
    parser.add_argument(
        "--nsfw_protection",
        type=int,
        help="Deactivate/Activate nsfw protection",
        default=0
    )


def read_resize_factor_parameter(parser):
    parser.add_argument(
        "--resize_factor",
        type=int,
        help="Resize factor",
        default=2
    )


def read_watermark_protection_parameter(parser):
    parser.add_argument(
        "--watermark_protection",
        type=int,
        help="Deactivate/Activate watermark protection",
        default=0
    )


def main():
    # Read parameters from command line
    parser = argparse.ArgumentParser()
    read_prompt_parameter(parser)
    read_outdir_parameter(parser)
    read_skip_grid_parameter(parser)
    read_skip_save_parameter(parser)
    read_ddim_steps_parameter(parser)
    read_plms_parameter(parser)
    read_dpm_solver_parameter(parser)
    read_laion400m_parameter(parser)
    read_fixed_code_parameter(parser)
    read_ddim_eta_parameter(parser)
    read_n_iter_parameter(parser)
    read_height_parameter(parser)
    read_width_parameter(parser)
    read_latent_channels_parameter(parser)
    read_downsampling_factor_parameter(parser)
    read_n_samples_parameter(parser)
    read_n_rows_parameter(parser)
    read_scale_parameter(parser)
    read_from_file_parameter(parser)
    read_config_parameter(parser)
    read_ckpt_parameter(parser)
    read_seed_parameter(parser)
    read_precision_parameter(parser)
    read_nsfw_protection_parameter(parser)
    read_resize_factor_parameter(parser)
    read_watermark_protection_parameter(parser)
    opt = parser.parse_args()

    if opt.laion400m:
        print("Falling back to LAION 400M model...")
        opt.config = "configs/latent-diffusion/txt2img-1p4B-eval.yaml"
        opt.ckpt = "models/ldm/text2img-large/model.ckpt"
        opt.outdir = "outputs/txt2img-samples-laion400m"

    seed_everything(opt.seed)

    config = OmegaConf.load(f"{opt.config}")
    model = load_model_from_config(config, f"{opt.ckpt}")

    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    model = model.to(device)
    model.half()

    if opt.dpm_solver:
        sampler = DPMSolverSampler(model)
    elif opt.plms:
        sampler = PLMSSampler(model)
    else:
        sampler = DDIMSampler(model)

    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir

    print("Creating invisible watermark encoder (see https://github.com/ShieldMnt/invisible-watermark)...")
    wm = "StableDiffusionV1"
    wm_encoder = WatermarkEncoder()
    wm_encoder.set_watermark('bytes', wm.encode('utf-8'))

    batch_size = opt.n_samples
    n_rows = opt.n_rows if opt.n_rows > 0 else batch_size
    if not opt.from_file:
        prompt = opt.prompt
        assert prompt is not None
        data = [batch_size * [prompt]]

    else:
        print(f"reading prompts from {opt.from_file}")
        with open(opt.from_file, "r") as f:
            data = f.read().splitlines()
            data = list(chunk(data, batch_size))

    sample_path = os.path.join(outpath, "samples")
    # Folder with the original output
    os.makedirs(os.path.join(sample_path, "original"), exist_ok=True)
    # Folder with the resized output
    os.makedirs(os.path.join(sample_path, "resized"), exist_ok=True)
    # Folder with the improved output based on the resized output
    os.makedirs(os.path.join(sample_path, "improved"), exist_ok=True)
    base_count = len(os.listdir(os.path.join(sample_path, "original")))
    grid_count = len(os.listdir(outpath)) - 1

    start_code = None
    if opt.fixed_code:
        start_code = torch.randn([opt.n_samples, opt.C, opt.H // opt.f, opt.W // opt.f], device=device)

    precision_scope = autocast if opt.precision=="autocast" else nullcontext
    with torch.no_grad():
        with precision_scope("cuda"):
            with model.ema_scope():
                tic = time.time()
                all_samples = list()
                for n in trange(opt.n_iter, desc="Sampling"):
                    for prompts in tqdm(data, desc="data"):
                        uc = None
                        if opt.scale != 1.0:
                            uc = model.get_learned_conditioning(batch_size * [""])
                        if isinstance(prompts, tuple):
                            prompts = list(prompts)
                        c = model.get_learned_conditioning(prompts)
                        shape = [opt.C, opt.H // opt.f, opt.W // opt.f]
                        samples_ddim, _ = sampler.sample(S=opt.ddim_steps,
                                                         conditioning=c,
                                                         batch_size=opt.n_samples,
                                                         shape=shape,
                                                         verbose=False,
                                                         unconditional_guidance_scale=opt.scale,
                                                         unconditional_conditioning=uc,
                                                         eta=opt.ddim_eta,
                                                         x_T=start_code)


                        x_samples_ddim = model.decode_first_stage(samples_ddim)
                        x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
                        x_samples_ddim = x_samples_ddim.cpu().permute(0, 2, 3, 1).numpy()

                        x_checked_image, has_nsfw_concept = check_safety(bool(opt.nsfw_protection), x_samples_ddim)

                        x_checked_image_torch = torch.from_numpy(x_checked_image).permute(0, 3, 1, 2)

                        if not opt.skip_save:
                            for x_sample in x_checked_image_torch:
                                x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                                img = Image.fromarray(x_sample.astype(np.uint8))
                                img = put_watermark(opt.watermark_protection, img, wm_encoder)
                                img.save(os.path.join(sample_path, f"original\\{base_count:05}.png"))
                                resize_image(os.path.join(sample_path, f"original\\{base_count:05}.png")
                                             , os.path.join(sample_path, f"resized\\{base_count:05}.png")
                                             , opt.W, opt.H, opt.resize_factor)
                                improve_image(os.path.join(sample_path, f"resized\\{base_count:05}.png")
                                              , os.path.join(sample_path, f"improved\\{base_count:05}.png"))
                                base_count += 1

                        if not opt.skip_grid:
                            all_samples.append(x_checked_image_torch)

                if not opt.skip_grid:
                    # additionally, save as grid
                    grid = torch.stack(all_samples, 0)
                    grid = rearrange(grid, 'n b c h w -> (n b) c h w')
                    grid = make_grid(grid, nrow=n_rows)

                    # to image
                    grid = 255. * rearrange(grid, 'c h w -> h w c').cpu().numpy()
                    img = Image.fromarray(grid.astype(np.uint8))
                    img = put_watermark(opt.watermark_protection, img, wm_encoder)
                    img.save(os.path.join(outpath, f'grid-{grid_count:04}.png'))
                    grid_count += 1

                toc = time.time()

    print(f"Your samples are ready and waiting for you here: \n{outpath} \n"
          f" \nEnjoy.")


if __name__ == "__main__":
    main()
