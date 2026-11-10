"""Smoke test on a small synthetic file laid out like a MONC 3-D diagnostic (two records, warm cloud only)."""
import json
import numpy as np
import pytest

netCDF4 = pytest.importorskip('netCDF4')

from monc_render3d import MoncFile, Settings, render_file
from monc_render3d.plan import plan
from monc_render3d.cli import render_jobs
from monc_render3d.assemble import assemble


def make_file(path, times, nx=24, ny=20, nz=30, dx=100.0):
    d = netCDF4.Dataset(path, 'w')
    tdim = 'time_series_60_120'
    for name, n in ((tdim, len(times)), ('x', nx), ('y', ny), ('z', nz), ('zn', nz)):
        d.createDimension(name, n)
    d.setncattr('MONC time', f'{times[-1]:.2f}')
    d.createVariable(tdim, 'f8', (tdim,))[:] = times
    zn = np.linspace(0, 3000, nz); d.createVariable('zn', 'f8', ('zn',))[:] = zn
    d.createVariable('z', 'f8', ('z',))[:] = zn
    d.createVariable('x_resolution', 'f4', (tdim,))[:] = dx
    x, y, z = np.meshgrid(np.arange(nx), np.arange(ny), zn, indexing='ij')
    q = d.createVariable('q_cloud_liquid_mass', 'f4', (tdim, 'x', 'y', 'zn'))
    for r, t in enumerate(times):   # a bubble of cloud that drifts with time
        bubble = np.exp(-(((x - 8 - r) / 3) ** 2 + ((y - 10) / 3) ** 2 + ((z - 1200) / 300) ** 2))
        q[r] = 1e-3 * bubble
    d.createVariable('q_vapour', 'f4', (tdim, 'x', 'y', 'zn'))[:] = 0.01
    d.close()


def test_reader_and_frame(tmp_path):
    f = tmp_path / 'case_diagnostic_3d_120.nc'
    make_file(f, [60.0, 120.0])
    with MoncFile(f) as m:
        assert m.condensates() == ['q_cloud_liquid_mass']
        assert list(m.times()) == [60.0, 120.0]
        assert m.spacing() == (100.0, 100.0)
    s = Settings(size=(160, 110, 90))
    im, t = render_file(f, 1, s, label='test')
    assert t == 120.0 and im.size == (160, 110)
    a = np.asarray(im).astype(float)
    assert a.std() > 1.0          # not a blank image


def test_pipeline(tmp_path):
    for k in range(3):
        make_file(tmp_path / f'case_diagnostic_3d_{(k + 1) * 120}.nc', [k * 120 + 60.0, (k + 1) * 120.0])
    cfg = dict(runs=[dict(key='a', label='Run A', files=[str(tmp_path / 'case_diagnostic_3d_*.nc')]),
                     dict(key='b', label='Run B', files=[str(tmp_path / 'case_diagnostic_3d_*.nc')])],
               stills_count=2, render=dict(size=[160, 110, 90]))
    out = tmp_path / 'out'
    p = plan(cfg, out)
    assert len(p['movie']) == 6 and p['step_s'] == 60.0
    render_jobs(out, procs=1)
    assemble(out, ffmpeg='none')
    assert (out / 'out' / 'movie.gif').exists() and (out / 'out' / 'snapshots_grid.png').exists()
    assert len(json.loads((out / 'jobs.json').read_text())) == 2 * 6 + 2 * 2
