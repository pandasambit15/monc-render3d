"""Index MONC runs and plan the frames, stills and clips to render.

A configuration lists one or more runs (one panel each); each run is a list of glob patterns. Files are opened to
read their record times, so any MONC file naming works and multi-record files are expanded record by record.
Within a run, later globs (and later files within a glob, in sorted order) replace earlier ones at the same time,
so a run continued over several folders is just several globs.
"""
import glob, json, re
from collections import Counter
from pathlib import Path
import numpy as np

from .monc_io import MoncFile
from .render import Settings, auto_ztop


def index_run(globs, time_regex=None):
    """{time (s): (file, record)} for one run. With time_regex, the time is read from the file name instead (one
    record per file; faster for thousands of files)."""
    out = {}
    rx = re.compile(time_regex) if time_regex else None
    for g in globs:
        for p in sorted(glob.glob(g)):
            if rx:
                m = rx.search(p)
                if m:
                    out[float(m[1])] = (p, 0)
                continue
            try:
                with MoncFile(p) as f:
                    for r, t in enumerate(f.times()):
                        out[float(t)] = (p, r)
            except (OSError, ValueError) as e:
                print(f'monc_render3d: skipping {p}: {e}', flush=True)
    return out


def output_step(times):
    """The most common interval between consecutive output times."""
    d = np.round(np.diff(sorted(times)), 6)
    return float(Counter(d.tolist()).most_common(1)[0][0]) if d.size else 0.0


def continuous_end(times, step):
    ts = sorted(times); end = ts[0]
    for a, b in zip(ts, ts[1:]):
        if abs((b - a) - step) > 1e-6 * max(step, 1):
            break
        end = b
    return end


def time_unit(span_s, unit='auto'):
    if unit != 'auto':
        return unit
    return 'day' if span_s >= 2 * 86400 else 'h' if span_s >= 3 * 3600 else 'min' if span_s >= 180 else 's'


def fmt_time(t, unit):
    f = dict(day=86400, h=3600, min=60, s=1)[unit]
    v = t / f
    return f'{v:.2f} {unit}' if unit in ('day', 'h') else f'{v:.0f} {unit}'


def _times_from(spec, all_times, unit_factor):
    return sorted({min(all_times, key=lambda t: abs(t - v * unit_factor)) for v in spec}) if all_times else []


def plan(config, outdir):
    """Write OUTDIR/jobs.json and OUTDIR/products.json; return the products dict.

    config keys: runs [{key, label, files: [globs]}], optional title, tag, window ("common" | "union"),
    time_regex, stills (seconds) or stills_days / stills_hours, stills_count, phases [{key, label, start, end}]
    in seconds (or [key, label, first_day, last_day]), time_unit, render (Settings fields).
    """
    out = Path(outdir).resolve(); out.mkdir(parents=True, exist_ok=True)
    runs = config['runs']
    assert runs, 'config has no runs'
    idx = {r['key']: index_run(r['files'], config.get('time_regex')) for r in runs}
    for r in runs:
        assert idx[r['key']], f"run {r['key']}: no MONC 3-D files matched {r['files']}"
    step = min(output_step(v) for v in idx.values()) or 1.0
    info = {k: dict(n=len(v), first=min(v), last=max(v), continuous_end=continuous_end(v, step)) for k, v in idx.items()}
    if config.get('window', 'common') == 'union':
        movie = sorted(set().union(*[set(v) for v in idx.values()]))
    else:
        # the longest unbroken stretch (one output step apart) of the times every run has; a stray early or late
        # file, or a gap, does not truncate the movie
        common = sorted(set.intersection(*[set(v) for v in idx.values()]))
        movie, cur = [], []
        for t in common:
            cur = cur + [t] if cur and abs((t - cur[-1]) - step) <= 1e-6 * max(step, 1) else [t]
            if len(cur) > len(movie):
                movie = cur
    assert movie, 'no output time is shared by all runs; try "window": "union"'
    unit = time_unit(movie[-1] - movie[0], config.get('time_unit', 'auto'))
    alltimes = sorted(set().union(*[set(v) for v in idx.values()]))
    # stills: explicit times, else evenly spaced over the movie window
    if 'stills' in config:
        stills = _times_from(config['stills'], alltimes, 1)
    elif 'stills_days' in config:
        stills = _times_from(config['stills_days'], alltimes, 86400)
    elif 'stills_hours' in config:
        stills = _times_from(config['stills_hours'], alltimes, 3600)
    else:
        n = int(config.get('stills_count', 6))
        stills = sorted({movie[int(round(i))] for i in np.linspace(0, len(movie) - 1, min(n, len(movie)))})
    phases = []
    for p in config.get('phases', []):
        if isinstance(p, (list, tuple)):
            key, label, a, b = p; a, b = a * 86400, b * 86400
        else:
            key, label, a, b = p['key'], p.get('label', p['key']), p['start'], p['end']
        ts = [t for t in alltimes if a - 1e-6 <= t <= b + 1e-6]
        phases.append(dict(key=key, label=label, times=ts, available={k: sum(t in idx[k] for t in ts) for k in idx}))
    # render settings; a fixed render top for the whole product set
    s = Settings.from_dict(config.get('render'))
    if not s.ztop:
        sample = []
        for k, v in idx.items():
            ts = sorted(v); picks = [ts[int(i)] for i in np.linspace(0, len(ts) - 1, min(12, len(ts)))]
            sample += [v[t] for t in picks]
        s.ztop = auto_ztop(sample, s)
    stills_only = bool(config.get('stills_only'))
    need = set() if stills_only else set(movie) | {t for p in phases for t in p['times']}
    tag = config.get('tag', '')
    jobs = []
    for r in runs:
        k, label = r['key'], r.get('label', r['key'])
        for t in sorted(need):
            if t in idx[k]:
                f, rec = idx[k][t]
                jobs.append(dict(file=f, record=rec, t=t, png=str(out / 'frames' / k / f'{t:012.1f}.png'), label=label + tag))
        for t in stills:
            if t in idx[k]:
                f, rec = idx[k][t]
                jobs.append(dict(file=f, record=rec, t=t, png=str(out / 'stills' / k / f'{t:012.1f}_x2.png'),
                                 label=f'{label}, {fmt_time(t, unit)}{tag}', scale=2.0))
    with MoncFile(next(iter(idx[runs[0]['key']].values()))[0]) as f:
        grid = f.summary()
    products = dict(title=config.get('title'), tag=tag, stills_only=stills_only, runs=[[r['key'], r.get('label', r['key'])] for r in runs],
                    sources=info, step_s=step, time_unit=unit, movie=movie, phases=phases, stills=stills,
                    settings=s.to_dict(), grid=dict(shape=grid['shape'], dx=grid['dx'], dy=grid['dy'],
                    domain_m=grid['domain_m'], top_m=grid['top_m']),
                    files={k: {str(t): list(v) for t, v in idx[k].items()} for k in idx})
    (out / 'jobs.json').write_text(json.dumps(jobs, indent=0))
    (out / 'products.json').write_text(json.dumps(products, indent=0))
    for k, i in info.items():
        print(f"{k}: {i['n']} records, {fmt_time(i['first'], unit)} - {fmt_time(i['last'], unit)}, "
              f"continuous to {fmt_time(i['continuous_end'], unit)}")
    print(f"movie: {len(movie)} frames, {fmt_time(movie[0], unit)} - {fmt_time(movie[-1], unit)}, step {step:g} s; "
          f"render top {s.ztop:g} m; {len(jobs)} render jobs ({sum('scale' in j for j in jobs)} stills)")
    return products
