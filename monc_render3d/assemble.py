"""Assemble rendered panels into the movie, phase clips, GIFs and the snapshot grid listed in products.json.

out/movie.mp4 and .gif: all runs side by side over the movie window; out/phase_<key>.mp4/.gif and one GIF per run and
phase; out/snapshots_grid.png: runs (rows) x still times (columns) from the double-size stills. A panel with no
output at a time is drawn grey and marked "no output". MP4 needs an ffmpeg binary; GIFs and PNGs do not.
"""
import shutil, subprocess
from pathlib import Path
import json
from PIL import Image, ImageDraw

from .plan import fmt_time
from .render import font


def find_ffmpeg(choice='auto'):
    """'none' -> None; a path -> that path; 'auto' -> ffmpeg on PATH, else imageio-ffmpeg's bundled binary."""
    if choice == 'none':
        return None
    if choice != 'auto':
        return choice
    exe = shutil.which('ffmpeg')
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def default_title(p):
    g = p['grid']; nx, ny, nz = g['shape']
    km = lambda m: f'{m / 1000:g} km' if m >= 1000 else f'{m:g} m'
    return f"{nx} x {ny} x {nz} grid, dx {km(g['dx'])}, domain {km(g['domain_m'][0])} x {km(g['domain_m'][1])}"


def assemble(outdir, ffmpeg='auto', movie_fps=6, phase_fps=3, gif_scale=0.5, encode_only=False):
    N = Path(outdir).resolve(); P = json.loads((N / 'products.json').read_text())
    OUT = N / 'out'; OUT.mkdir(exist_ok=True)
    RUNS = P['runs']; unit = P['time_unit']
    W, H = P['settings']['size'][:2]; TB = 44
    title = P.get('title') or default_title(P)
    ff = find_ffmpeg(ffmpeg)

    def missing(w, h, name, scale=1.0):
        im = Image.new('RGB', (w, h), (70, 74, 80)); d = ImageDraw.Draw(im)
        d.text((int(14 * scale), int(10 * scale)), name + P.get('tag', ''), fill=(235, 235, 235), font=font(int(20 * scale)))
        d.text((w // 2, h // 2), 'no output at this time', fill=(220, 220, 220), font=font(int(22 * scale)), anchor='mm')
        return im

    def panel(k, name, t):
        f = N / 'frames' / k / f'{t:012.1f}.png'
        return Image.open(f).convert('RGB') if f.exists() else missing(W, H, name)

    def combined(t, subtitle):
        im = Image.new('RGB', (W * len(RUNS), H + TB), (18, 24, 34)); d = ImageDraw.Draw(im)
        for i, (k, name) in enumerate(RUNS):
            im.paste(panel(k, name, t), (i * W, TB))
        d.text((14, TB // 2), f'{title}.  {subtitle}' if subtitle else title, fill=(235, 235, 235), font=font(20), anchor='lm')
        d.text((W * len(RUNS) - 14, TB // 2), fmt_time(t, unit), fill=(255, 255, 255), font=font(22), anchor='rm')
        return im

    def write(frames_dir, stem, fps, gif_every):
        files = sorted(frames_dir.glob('*.png'))
        if not files:
            return
        if ff:
            subprocess.run([ff, '-y', '-loglevel', 'error', '-framerate', str(fps), '-pattern_type', 'glob', '-i',
                            str(frames_dir / '*.png'), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '20',
                            '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', str(OUT / f'{stem}.mp4')], check=True)
        gif = [Image.open(f).convert('RGB') for f in files[::gif_every]]
        gif = [g.resize((int(g.width * gif_scale), int(g.height * gif_scale)), Image.LANCZOS) for g in gif]
        gif[0].save(OUT / f'{stem}.gif', save_all=True, append_images=gif[1:], duration=int(1000 * gif_every / fps),
                    loop=0, optimize=True)

    movie = P['movie']
    d = N / 'combined' / 'movie'; d.mkdir(parents=True, exist_ok=True)
    if not (encode_only and len(list(d.glob('*.png'))) == len(movie)):
        sub = f'{fmt_time(movie[0], unit)} - {fmt_time(movie[-1], unit)}'
        for k, t in enumerate(movie):
            combined(t, sub).save(d / f'{k:05d}.png')
    write(d, 'movie', movie_fps, 2 if len(movie) > 200 else 1)
    for ph in P['phases']:
        d = N / 'combined' / ph['key']; d.mkdir(parents=True, exist_ok=True)
        if not (encode_only and len(list(d.glob('*.png'))) == len(ph['times'])):
            for k, t in enumerate(ph['times']):
                combined(t, ph['label']).save(d / f'{k:05d}.png')
        write(d, f"phase_{ph['key']}", phase_fps, 1)
        for k, name in RUNS:
            fr = [N / 'frames' / k / f'{t:012.1f}.png' for t in ph['times']]
            fr = [Image.open(f).convert('RGB').resize((W * 3 // 4, H * 3 // 4), Image.LANCZOS) for f in fr if f.exists()]
            if fr:
                fr[0].save(OUT / f"phase_{ph['key']}_{k}.gif", save_all=True, append_images=fr[1:],
                           duration=int(1000 / phase_fps), loop=0, optimize=True)
    # snapshot grid: runs in rows, still times in columns
    if P['stills']:
        f22 = font(22)
        lw = max(170, 30 + max(int(f22.getlength(name)) for _, name in RUNS)); hh = 56   # fit the longest run label
        g = Image.new('RGB', (lw + W * len(P['stills']), hh + H * len(RUNS)), (255, 255, 255)); dr = ImageDraw.Draw(g)
        for j, t in enumerate(P['stills']):
            dr.text((lw + j * W + W // 2, hh // 2), fmt_time(t, unit), fill=(20, 20, 20), font=font(26), anchor='mm')
            for i, (k, name) in enumerate(RUNS):
                f = N / 'stills' / k / f'{t:012.1f}_x2.png'
                im = Image.open(f).convert('RGB').resize((W, H), Image.LANCZOS) if f.exists() else missing(W, H, name)
                g.paste(im, (lw + j * W, hh + i * H))
        for i, (k, name) in enumerate(RUNS):
            dr.text((lw // 2, hh + i * H + H // 2), name, fill=(20, 20, 20), font=font(22), anchor='mm')
        g.save(OUT / 'snapshots_grid.png')
    for f in sorted(OUT.iterdir()):
        print(f.name, f.stat().st_size // 1024, 'KB')
