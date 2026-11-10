"""Reading MONC 3-D diagnostic files.

MONC's IO server writes each 3-D diagnostic as (time_series_<sampling>_<write>, x, y, zn) (or z for w), with the
record times in a variable named after the time dimension, x_resolution / y_resolution per record, zn and z
coordinates, a global "MONC time" attribute and the run's options database. A file may hold several time records.
This module reads all of that with netCDF4 if it is installed, otherwise with h5py (netCDF-4/HDF5 files only).
"""
import re
from pathlib import Path
import numpy as np

# Condensate species MONC writes (CASIM and the simple cloud scheme share these names), with a nominal mass
# extinction coefficient (m2/kg, 1.5/(rho_w r_e) for effective radius r_e) and a display tint. Liquid at 10 um is
# opaque, ice at 30 um translucent, snow 250 um, graupel and rain 500 um. Order matters only for float summation.
SPECIES = {
    'q_cloud_liquid_mass': (150.0, (1.0, 1.0, 1.0)),
    'q_ice_mass': (50.0, (0.86, 0.93, 1.0)),
    'q_snow_mass': (6.0, (0.86, 0.93, 1.0)),
    'q_graupel_mass': (3.0, (0.8, 0.85, 0.95)),
    'q_rain_mass': (3.0, (0.55, 0.6, 0.7)),
}
DX_DEFAULT = 1000.0


def _text(v):
    v = np.asarray(v).ravel()[0] if not isinstance(v, (str, bytes)) else v
    return v.decode() if isinstance(v, bytes) else str(v)


class MoncFile:
    """One MONC 3-D diagnostic file. Usage: with MoncFile(path) as f: f.times(), f.read(name, record), ..."""

    def __init__(self, path):
        self.path = str(path)
        try:
            import netCDF4
            self._nc = netCDF4.Dataset(self.path, 'r')
            self._nc.set_auto_mask(False)
            self._h5 = None
        except ImportError:
            import h5py
            self._nc, self._h5 = None, h5py.File(self.path, 'r')

    # -- low level -------------------------------------------------------------------------------------------
    def close(self):
        (self._nc or self._h5).close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def variables(self):
        if self._nc is not None:
            return list(self._nc.variables)
        return [k for k, v in self._h5.items() if hasattr(v, 'shape')]

    def has(self, name):
        return name in self.variables()

    def dims(self, name):
        if self._nc is not None:
            return tuple(self._nc.variables[name].dimensions)
        d = self._h5[name]
        return tuple(Path(d.dims[i][0].name).name if len(d.dims[i]) else f'dim{i}' for i in range(d.ndim))

    def shape(self, name):
        return tuple((self._nc.variables if self._nc is not None else self._h5)[name].shape)

    def array(self, name, index=Ellipsis):
        v = (self._nc.variables if self._nc is not None else self._h5)[name]
        return np.asarray(v[index])

    def attr(self, name, default=None):
        if self._nc is not None:
            return _text(self._nc.getncattr(name)) if name in self._nc.ncattrs() else default
        return _text(self._h5.attrs[name]) if name in self._h5.attrs else default

    # -- MONC structure --------------------------------------------------------------------------------------
    def fields_3d(self):
        """Names of the variables with a time dimension followed by three spatial dimensions."""
        out = []
        for k in self.variables():
            d = self.dims(k)
            if len(d) == 4 and d[0].startswith('time') and d[1] == 'x' and d[2] == 'y':
                out.append(k)
        return out

    def condensates(self):
        """The condensate species of SPECIES present in this file, in SPECIES order."""
        present = set(self.fields_3d())
        return [k for k in SPECIES if k in present]

    def time_dim(self, name=None):
        name = name or (self.condensates() or self.fields_3d() or [None])[0]
        if name is None:
            raise ValueError(f'{self.path}: no 3-D (time, x, y, z) field found')
        return self.dims(name)[0]

    def times(self, name=None):
        """Model time (s) of every record. From the time-dimension variable; else the "MONC time" attribute (one
        record); else the last number in the file name."""
        tdim = self.time_dim(name)
        n = self.shape(name or (self.condensates() or self.fields_3d())[0])[0]
        if self.has(tdim):
            t = self.array(tdim).astype(float).ravel()
            if t.size == n:
                return t
        mt = self.attr('MONC time')
        if mt is not None and n == 1:
            return np.array([float(mt)])
        m = re.findall(r'(\d+(?:\.\d+)?)', Path(self.path).stem)
        if m and n == 1:
            return np.array([float(m[-1])])
        raise ValueError(f'{self.path}: cannot determine record times')

    def options(self):
        """The run's options database as a dict (empty if the file has none)."""
        if not self.has('options_database'):
            return {}
        try:
            raw = self.array('options_database')
            out = {}
            for kv in raw:
                key, val = (b''.join(np.atleast_1d(s)).decode(errors='ignore').strip('\x00 ').strip()
                            if np.asarray(s).dtype.kind in 'SU' else '' for s in kv)
                if key:
                    out[key] = val
            return out
        except Exception:
            return {}

    def spacing(self, record=0):
        """Horizontal grid spacing (dx, dy) in metres: x_resolution / y_resolution, else the options database
        (dxx, dyy), else 1000 m with a warning."""
        def get(var, opt):
            if self.has(var):
                v = self.array(var).ravel()
                return float(v[min(record, v.size - 1)])
            o = self.options().get(opt)
            try:
                return float(o) if o is not None else None
            except ValueError:
                return None
        dx = get('x_resolution', 'dxx')
        dy = get('y_resolution', 'dyy')
        if dx is None:
            print(f'monc_render3d: {self.path}: no x_resolution, assuming dx = {DX_DEFAULT} m', flush=True)
            dx = DX_DEFAULT
        return dx, (dy if dy is not None else dx)

    def heights(self, name='q_cloud_liquid_mass'):
        """Vertical coordinate (m) of a field: zn for cell-centre fields, z for w-level fields."""
        zdim = self.dims(name)[3] if self.has(name) else 'zn'
        for v in (zdim, 'zn', 'z'):
            if self.has(v):
                return self.array(v).astype(float).ravel()
        raise ValueError(f'{self.path}: no vertical coordinate')

    def read(self, name, record=0):
        """One record of a 3-D field as float32 (x, y, z)."""
        return self.array(name, (record, slice(None), slice(None), slice(None))).astype(np.float32)

    def summary(self):
        f3 = self.fields_3d()
        name = (self.condensates() or f3)[0]
        nx, ny, nz = self.shape(name)[1:]
        dx, dy = self.spacing()
        zn = self.heights(name)
        return dict(path=self.path, records=len(self.times()), times=self.times().tolist(), shape=[nx, ny, nz],
                    dx=dx, dy=dy, domain_m=[nx * dx, ny * dy], top_m=float(zn[-1]), condensates=self.condensates(),
                    fields_3d=f3)
