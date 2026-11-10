"""Command line: monc-render3d {inspect, frame, plan, render, assemble, run}. See README.md."""
import argparse, json, multiprocessing as mp, sys
from pathlib import Path

from .monc_io import MoncFile, SPECIES
from .render import Settings, render_file
from .plan import plan
from .assemble import assemble


def _settings_args(p):
    g = p.add_argument_group('render settings (defaults suit most MONC set-ups)')
    g.add_argument('--ztop', type=float, help='rendered height in m (default: from the condensate in the data)')
    g.add_argument('--dz', type=float, help='vertical sampling step in m (default: ztop / nlevels)')
    g.add_argument('--nlevels', type=int, help='vertical render levels when --dz is not given (default 90)')
    g.add_argument('--upsample', type=int, help='horizontal upsampling (default 2; use 1 for very large grids)')
    g.add_argument('--smooth', type=float, help='horizontal smoothing in grid lengths (default 0.7)')
    g.add_argument('--vexag', type=float, help='vertical exaggeration (default 1.6)')
    g.add_argument('--azimuth', type=float); g.add_argument('--elevation', type=float)
    g.add_argument('--sun-azimuth', type=float, dest='sun_azimuth'); g.add_argument('--sun-elevation', type=float, dest='sun_elevation')
    g.add_argument('--size', type=int, nargs=3, metavar=('W', 'H', 'D'), help='image width, height, depth samples')
    g.add_argument('--no-periodic', action='store_true', help='treat the horizontal boundaries as open')
    g.add_argument('--species', nargs='+', metavar='NAME[:KEXT]',
                   help=f'condensate fields to render (default: those of {list(SPECIES)} in the file); an unknown '
                        'name needs :KEXT, a mass extinction coefficient in m2/kg')


def _settings(args, base=None):
    d = dict(base or {})
    for k in ('ztop', 'dz', 'nlevels', 'upsample', 'smooth', 'vexag', 'azimuth', 'elevation', 'sun_azimuth', 'sun_elevation'):
        v = getattr(args, k, None)
        if v is not None:
            d[k] = v
    if getattr(args, 'size', None):
        d['size'] = args.size
    if getattr(args, 'no_periodic', False):
        d['periodic'] = False
    s = Settings.from_dict(d)
    if getattr(args, 'species', None):
        sp = {}
        for item in args.species:
            name, _, kext = item.partition(':')
            if kext:
                sp[name] = (float(kext), SPECIES.get(name, (0, (1.0, 1.0, 1.0)))[1])
            elif name in SPECIES:
                sp[name] = SPECIES[name]
            else:
                sys.exit(f'--species {name}: unknown field, give NAME:KEXT')
        s.species = sp
    return s


def _job(job):
    s = Settings.from_dict(job['settings'])
    im, t = render_file(job['file'], job.get('record', 0), s, job.get('label', ''), job.get('scale', 1.0))
    if 't' in job and abs(t - job['t']) > 1e-3:
        raise ValueError(f"{job['file']} record {job.get('record', 0)}: time {t} differs from the planned {job['t']}")
    Path(job['png']).parent.mkdir(parents=True, exist_ok=True)
    im.save(job['png'])
    return job['png']


def render_jobs(outdir, procs, stills_procs=None, redo=False):
    out = Path(outdir).resolve()
    jobs = json.loads((out / 'jobs.json').read_text())
    s = json.loads((out / 'products.json').read_text())['settings']
    todo = [dict(j, settings=s) for j in jobs if redo or not Path(j['png']).exists()]
    frames = [j for j in todo if 'scale' not in j]; stills = [j for j in todo if 'scale' in j]
    print(f'{len(frames)} frames and {len(stills)} stills to render ({len(jobs) - len(todo)} already done)', flush=True)
    for group, n in ((frames, procs), (stills, stills_procs or max(1, procs // 4))):
        if not group:
            continue
        with mp.Pool(max(1, n)) as pool:
            for k, p in enumerate(pool.imap_unordered(_job, group), 1):
                if k % 50 == 0 or k == len(group):
                    print(f'  {k}/{len(group)} {p}', flush=True)


def main(argv=None):
    a = argparse.ArgumentParser(prog='monc-render3d', description='3-D cloud volume renders of MONC 3-D diagnostics.')
    sub = a.add_subparsers(dest='cmd', required=True)
    i = sub.add_parser('inspect', help='show what the tool finds in MONC files')
    i.add_argument('files', nargs='+')
    f = sub.add_parser('frame', help='render one record of one file')
    f.add_argument('file'); f.add_argument('-o', '--out', required=True, help='output PNG')
    f.add_argument('--record', type=int, default=0, help='record index within the file (default 0)')
    f.add_argument('--label', default=''); f.add_argument('--scale', type=float, default=1.0)
    _settings_args(f)
    for name, hlp in (('plan', 'index runs and write jobs.json / products.json'),
                      ('run', 'plan, render and assemble in one go')):
        p = sub.add_parser(name, help=hlp)
        p.add_argument('-o', '--outdir', required=True)
        src = p.add_mutually_exclusive_group(required=True)
        src.add_argument('--config', help='JSON configuration (see examples/)')
        src.add_argument('--files', nargs='+', action='append', metavar='GLOB',
                         help='one run per --files (repeat for side-by-side panels); quote the globs')
        p.add_argument('--labels', nargs='+', help='panel labels for the --files runs')
        p.add_argument('--title'); p.add_argument('--window', choices=('common', 'union'))
        p.add_argument('--stills-count', type=int, dest='stills_count')
        _settings_args(p)
        if name == 'run':
            p.add_argument('--procs', type=int, default=max(1, mp.cpu_count() // 2))
            p.add_argument('--ffmpeg', default='auto', help='ffmpeg binary, "auto" or "none" (GIF/PNG only)')
    r = sub.add_parser('render', help='render the planned frames and stills (skips existing ones)')
    r.add_argument('outdir'); r.add_argument('--procs', type=int, default=max(1, mp.cpu_count() // 2))
    r.add_argument('--redo', action='store_true')
    s = sub.add_parser('assemble', help='build movie, clips, GIFs and the snapshot grid')
    s.add_argument('outdir'); s.add_argument('--ffmpeg', default='auto')
    s.add_argument('--movie-fps', type=int, default=6, dest='movie_fps')
    s.add_argument('--phase-fps', type=int, default=3, dest='phase_fps')
    s.add_argument('--encode-only', action='store_true', dest='encode_only')
    args = a.parse_args(argv)

    if args.cmd == 'inspect':
        for p in args.files:
            with MoncFile(p) as m:
                print(json.dumps({k: v for k, v in m.summary().items() if k != 'fields_3d'} |
                                 dict(n_fields_3d=len(m.fields_3d())), indent=1))
    elif args.cmd == 'frame':
        st = _settings(args)
        if not st.ztop:
            from .render import auto_ztop
            st.ztop = auto_ztop([(args.file, args.record)], st)
        im, t = render_file(args.file, args.record, st, args.label, args.scale)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True); im.save(args.out)
        print(f'{args.out}  (t = {t:g} s, render top {st.ztop:g} m)')
    elif args.cmd in ('plan', 'run'):
        if args.config:
            cfg = json.loads(Path(args.config).read_text())
        else:
            labels = args.labels or [f'run {k + 1}' for k in range(len(args.files))]
            cfg = dict(runs=[dict(key=f'run{k + 1}', label=labels[k] if k < len(labels) else f'run {k + 1}', files=g)
                             for k, g in enumerate(args.files)])
        for k in ('title', 'window', 'stills_count'):
            if getattr(args, k, None) is not None:
                cfg[k] = getattr(args, k)
        cfg['render'] = _settings(args, cfg.get('render')).to_dict()
        plan(cfg, args.outdir)
        if args.cmd == 'run':
            render_jobs(args.outdir, args.procs)
            assemble(args.outdir, args.ffmpeg)
    elif args.cmd == 'render':
        render_jobs(args.outdir, args.procs, redo=args.redo)
    elif args.cmd == 'assemble':
        assemble(args.outdir, args.ffmpeg, args.movie_fps, args.phase_fps, encode_only=args.encode_only)


if __name__ == '__main__':
    main()
