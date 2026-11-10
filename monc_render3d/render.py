"""Volume rendering of MONC cloud condensate.

Extinction is beta = rho_air(z) * sum_s k_s q_s on a regular grid (horizontal upsampling, light smoothing, a fixed
vertical step from 0 to ztop). Each voxel is lit by the sun (attenuated along the sun direction) plus a skylight
term; front-to-back compositing gives the image over a sky gradient and an ocean floor that carries the cloud
shadows. The camera is oblique orthographic with vertical exaggeration. The render is qualitative: colours mark the
condensate phase, not amounts. Horizontal boundaries are periodic by default, as in most MONC set-ups.
"""
from dataclasses import dataclass, field, asdict
import numpy as np
from scipy import ndimage
from PIL import Image, ImageDraw, ImageFont

from .monc_io import MoncFile, SPECIES

OCEAN = np.array([0.10, 0.22, 0.36]); SKY_TOP = np.array([0.33, 0.52, 0.78]); SKY_BOT = np.array([0.78, 0.86, 0.95])


def font(size):
    """DejaVu Sans via matplotlib if available, else Pillow's default font."""
    try:
        import matplotlib.font_manager as fm
        return ImageFont.truetype(fm.findfont(fm.FontProperties(family='DejaVu Sans')), size)
    except Exception:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


@dataclass
class Settings:
    ztop: float = None            # rendered height (m); None = the file's model top
    dz: float = None              # vertical sampling step (m); None = ztop / nlevels
    nlevels: int = 90
    upsample: int = 2             # horizontal upsampling of the condensate fields
    smooth: float = 0.7           # horizontal Gaussian smoothing, in model grid lengths (softens blocky columns)
    smooth_z: float = 0.6         # vertical smoothing, in render levels
    vexag: float = 1.6            # vertical exaggeration of the display
    min_aspect: float = 0.2       # raise vexag so the box is at least this tall relative to its width (0 = off)
    floor: str = 'auto'           # ocean floor: 'slab' (thin opaque volume), 'plane' (exact per-pixel), 'auto'
    azimuth: float = 35.0         # camera azimuth (deg)
    elevation: float = 28.0       # camera elevation (deg)
    sun_azimuth: float = 120.0
    sun_elevation: float = 50.0
    size: tuple = (640, 420, 360)  # image width, height and depth samples at scale 1
    periodic: bool = True
    rho0: float = 1.17            # air density profile rho0 * exp(-z / H) used to weight mixing ratios (kg/m3)
    rho_scale_height: float = 8500.0
    species: dict = field(default_factory=lambda: dict(SPECIES))   # name -> (mass extinction m2/kg, (r, g, b))

    def resolved(self, zn):
        ztop = float(self.ztop) if self.ztop else float(zn[-1])
        dz = float(self.dz) if self.dz else ztop / self.nlevels
        return ztop, dz

    def to_dict(self):
        d = asdict(self); d['species'] = {k: [v[0], list(v[1])] for k, v in self.species.items()}
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        if 'species' in d:
            d['species'] = {k: (float(v[0]), tuple(v[1])) for k, v in d['species'].items()}
        if 'size' in d:
            d['size'] = tuple(d['size'])
        return cls(**d)


def load(path, record, s):
    """Condensate fields of one record: (zn, dx, {name: (x, y, z) float32}, time). Species named in the settings
    but absent from the file are skipped (a warm-rain or reduced output simply renders fewer species)."""
    with MoncFile(path) as f:
        present = [k for k in s.species if f.has(k)]
        if not present:
            raise ValueError(f'{path}: none of the configured condensate fields {list(s.species)} is in the file')
        zn = f.heights(present[0])
        dx, _ = f.spacing(record)
        q = {k: f.read(k, record) for k in present}
        t = float(f.times(present[0])[record])
    return zn, dx, q, t


def world(zn, dx, q, s):
    """Extinction (1/m) and phase tint on the regular render grid, and the render grid spacing (m)."""
    ztop, dz = s.resolved(zn)
    up = s.upsample
    zr = np.arange(dz / 2, ztop, dz)
    k0 = np.clip(np.searchsorted(zn, zr) - 1, 0, len(zn) - 2); w = ((zr - zn[k0]) / (zn[k0 + 1] - zn[k0])).astype(np.float32)
    rho = (s.rho0 * np.exp(-zr / s.rho_scale_height)).astype(np.float32)
    wrap = 'grid-wrap' if s.periodic else 'nearest'
    gmode = ('wrap', 'wrap', 'nearest') if s.periodic else ('nearest', 'nearest', 'nearest')
    beta = 0.0; tint = 0.0
    for name, (kext, colour) in s.species.items():
        if name not in q:
            continue
        a = q[name][:, :, k0] * (1 - w) + q[name][:, :, k0 + 1] * w
        a = ndimage.zoom(np.clip(a, 0, None), (up, up, 1), order=1, mode=wrap)
        a = ndimage.gaussian_filter(a, (s.smooth * up, s.smooth * up, s.smooth_z), mode=gmode)
        b = kext * rho * a; beta = beta + b; tint = tint + b[..., None] * np.array(colour, np.float32)
    tint = tint / np.maximum(beta, 1e-12)[..., None]
    return beta.astype(np.float32), tint.astype(np.float32), dx / up, dz


def sunlight(beta, h, dz, s):
    """Transmittance to the sun for every voxel and the floor, marching down from the top."""
    shift = dz / np.tan(np.radians(s.sun_elevation)) / h
    sx, sy = shift * np.cos(np.radians(s.sun_azimuth)), shift * np.sin(np.radians(s.sun_azimuth))
    mode = 'grid-wrap' if s.periodic else 'nearest'
    tau = np.zeros(beta.shape, np.float32); acc = np.zeros(beta.shape[:2], np.float32)
    for k in range(beta.shape[2] - 1, -1, -1):
        acc = ndimage.shift(acc, (sx, sy), order=1, mode=mode) + beta[:, :, k] * dz / np.sin(np.radians(s.sun_elevation))
        tau[:, :, k] = acc
    floor = ndimage.shift(acc, (sx, sy), order=1, mode=mode)
    up = np.cumsum(beta[:, :, ::-1], axis=2)[:, :, ::-1] * dz
    return np.exp(-tau), np.exp(-floor), up


def render_fields(zn, dx, q, s, label='', scale=1.0):
    W, H, D = (int(round(v * scale)) for v in s.size)
    beta, tint, h, dz = world(zn, dx, q, s)
    tsun, tfloor, up = sunlight(beta, h, dz, s)
    light = 1.0 * tsun + 0.45 * np.exp(-0.08 * up) + 0.15                      # direct + skylight + floor bounce
    nx, ny, nz = beta.shape
    a, e = np.radians(s.azimuth), np.radians(s.elevation)
    # vertical exaggeration: at least s.vexag, raised for very flat boxes so shallow cloud is resolved
    VE = max(s.vexag, s.min_aspect * max(nx, ny) * h / (nz * dz)) if s.min_aspect else s.vexag
    NF = 4                                                                      # floor layers below the box (slab)
    fl = (OCEAN * (0.35 + 0.65 * tfloor)[..., None]).astype(np.float32)
    floor = s.floor
    if floor == 'auto':
        # the slab floor needs several depth samples across its thickness, or it aliases into stripes
        Lz = (nz + NF) * dz * VE
        ds = 2 * 1.02 * np.sqrt((nx * h) ** 2 + (ny * h) ** 2 + Lz ** 2) / 2 / D
        floor = 'slab' if NF * dz * VE >= 8 * ds * np.sin(e) else 'plane'
    if floor == 'slab':
        # floor extinction: vertical optical depth 8 whatever the level spacing (1e-2 /m at dz = 200 m)
        beta_floor = max(1e-2, 8.0 / (NF * dz))
        beta = np.concatenate([np.full((nx, ny, NF), beta_floor, np.float32), beta], axis=2)
        colour = np.concatenate([np.repeat(fl[:, :, None, :], NF, axis=2), (light[..., None] * tint)], axis=2)
        nf = NF
    else:
        colour = light[..., None] * tint
        nf = 0
    Lx, Ly, Lz = nx * h, ny * h, (nz + nf) * dz * VE
    fwd = np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), -np.sin(e)])
    right = np.array([-np.sin(a), np.cos(a), 0.0]); upv = np.cross(fwd, right)
    centre = np.array([Lx / 2, Ly / 2, Lz / 2]); span = 1.02 * np.sqrt(Lx ** 2 + Ly ** 2 + Lz ** 2) / 2
    px = 2 * span / W; ds = 2 * span / D
    S = np.array([h, h, dz * VE])
    M = np.stack([-upv * px, right * px, fwd * ds], axis=1) / S[:, None]
    off = (centre - (H / 2) * (-upv) * px - (W / 2) * right * px - (D / 2) * fwd * ds) / S - 0.5
    samp = lambda v: ndimage.affine_transform(v, M, offset=off, output_shape=(H, W, D), order=1, mode='constant', cval=0.0)
    b = samp(beta)
    alpha = 1 - np.exp(-b * ds / VE)                                            # path length in true metres
    trans = np.cumprod(np.concatenate([np.ones((H, W, 1), np.float32), 1 - alpha[:, :, :-1]], axis=2), axis=2)
    wgt = trans * alpha
    img = np.stack([np.sum(wgt * samp(colour[..., c]), axis=2) for c in range(3)], axis=2)
    rest = trans[:, :, -1] * (1 - alpha[:, :, -1])
    sky = SKY_TOP + (SKY_BOT - SKY_TOP) * np.linspace(0, 1, H)[:, None, None]
    if floor == 'slab':
        background = np.broadcast_to(sky, (H, W, 3))
    else:
        # exact floor: where each pixel's ray meets z = 0 inside the domain, the shadowed ocean, else sky
        r, c = np.mgrid[0:H, 0:W]
        p0 = (centre[None, None, :] + ((r - H / 2) * px)[..., None] * (-upv) + ((c - W / 2) * px)[..., None] * right
              - (D / 2) * ds * fwd)
        tt = -p0[..., 2] / fwd[2]
        hx, hy = p0[..., 0] + tt * fwd[0], p0[..., 1] + tt * fwd[1]
        inside = (hx >= 0) & (hx <= Lx) & (hy >= 0) & (hy <= Ly)
        fc = np.stack([ndimage.map_coordinates(fl[..., k], [hx / h - 0.5, hy / h - 0.5], order=1,
                                               mode='grid-wrap' if s.periodic else 'nearest') for k in range(3)], axis=-1)
        background = np.where(inside[..., None], fc, np.broadcast_to(sky, (H, W, 3)))
    img = img + rest[..., None] * background
    img = np.clip(img, 0, 1) ** (1 / 1.1)
    im = Image.fromarray((img * 255).astype(np.uint8))
    if label:
        d = ImageDraw.Draw(im); d.text((int(14 * scale), int(10 * scale)), label, fill=(255, 255, 255), font=font(int(20 * scale)))
    return im


def render_file(path, record=0, settings=None, label='', scale=1.0):
    """Render one record of one MONC 3-D diagnostic file. Returns (PIL image, model time in s)."""
    s = settings or Settings()
    zn, dx, q, t = load(path, record, s)
    return render_fields(zn, dx, q, s, label, scale), t


def auto_ztop(paths_records, settings=None, threshold=1e-6, margin=1.08):
    """A render top that holds the condensate of the sampled records: the highest model level where any configured
    species exceeds `threshold` (kg/kg), times `margin`, rounded up to a round number and capped at the model top."""
    s = settings or Settings()
    top, model_top = 0.0, None
    for path, record in paths_records:
        with MoncFile(path) as f:
            present = [k for k in s.species if f.has(k)]
            zn = f.heights(present[0]); model_top = float(zn[-1])
            total = sum(f.read(k, record) for k in present)
            levels = np.where(total.max(axis=(0, 1)) > threshold)[0]
            if levels.size:
                top = max(top, float(zn[levels[-1]]))
    if model_top is None:
        raise ValueError('no files to sample')
    if top == 0.0:
        return model_top
    top *= margin
    step = 10 ** np.floor(np.log10(top)) / 2
    return float(min(np.ceil(top / step) * step, model_top))
