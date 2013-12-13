import fcntl
import logging
import os
import subprocess
import shutil
import sys
import tempfile
import time
import yaml
import beanstalkc

from datetime import datetime

from . import report
from . import safepath
from .config import config as teuth_config
from .misc import read_config

log = logging.getLogger(__name__)
start_time = datetime.utcnow()
restart_file_path = '/tmp/teuthology-restart-workers'


def need_restart():
    if not os.path.exists(restart_file_path):
        return False
    file_mtime = datetime.utcfromtimestamp(os.path.getmtime(restart_file_path))
    if file_mtime > start_time:
        return True
    else:
        return False


def restart():
    log.info('Restarting...')
    args = sys.argv[:]
    args.insert(0, sys.executable)
    os.execv(sys.executable, args)


class filelock(object):
    # simple flock class
    def __init__(self, fn):
        self.fn = fn
        self.fd = None

    def acquire(self):
        assert not self.fd
        self.fd = file(self.fn, 'w')
        fcntl.lockf(self.fd, fcntl.LOCK_EX)

    def release(self):
        assert self.fd
        fcntl.lockf(self.fd, fcntl.LOCK_UN)
        self.fd = None


def connect(ctx):
    host = ctx.teuthology_config['queue_host']
    port = ctx.teuthology_config['queue_port']
    return beanstalkc.Connection(host=host, port=port)


def fetch_teuthology_branch(path, branch='master'):
    """
    Make sure we have the correct teuthology branch checked out and up-to-date
    """
    # only let one worker create/update the checkout at a time
    lock = filelock('%s.lock' % path)
    lock.acquire()
    try:
        if not os.path.isdir(path):
            log.info("Cloning %s from upstream", branch)
            teuthology_git_upstream = teuth_config.ceph_git_base_url + \
                'teuthology.git'
            log.info(
                subprocess.check_output(('git', 'clone', '--branch', branch,
                                         teuthology_git_upstream, path),
                                        cwd=os.path.dirname(path))
            )
        elif time.time() - os.stat('/etc/passwd').st_mtime > 60:
            # only do this at most once per minute
            log.info("Fetching %s from upstream", branch)
            log.info(
                subprocess.check_output(('git', 'fetch', '-p', 'origin'),
                                        cwd=path)
            )
            log.info(
                subprocess.check_output(('touch', path))
            )
        else:
            log.info("%s was just updated; assuming it is current", branch)

        # This try/except block will notice if the requested branch doesn't
        # exist, whether it was cloned or fetched.
        try:
            subprocess.check_output(
                ('git', 'reset', '--hard', 'origin/%s' % branch),
                cwd=path,
            )
        except subprocess.CalledProcessError:
            log.exception("teuthology branch not found: %s", branch)
            shutil.rmtree(path)
            raise

        log.info("Bootstrapping %s", path)
        # This magic makes the bootstrap script not attempt to clobber an
        # existing virtualenv. But the branch's bootstrap needs to actually
        # check for the NO_CLOBBER variable.
        env = os.environ.copy()
        env['NO_CLOBBER'] = '1'
        log.info(
            subprocess.check_output(('./bootstrap'), cwd=path, env=env)
        )

    finally:
        lock.release()


def worker(ctx):
    loglevel = logging.INFO
    if ctx.verbose:
        loglevel = logging.DEBUG
    log.setLevel(loglevel)

    log_file_path = os.path.join(ctx.log_dir, 'worker.{tube}.{pid}'.format(
        pid=os.getpid(), tube=ctx.tube,))
    log_handler = logging.FileHandler(filename=log_file_path)
    log_formatter = logging.Formatter(
        fmt='%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S')
    log_handler.setFormatter(log_formatter)
    log.addHandler(log_handler)

    if not os.path.isdir(ctx.archive_dir):
        sys.exit("{prog}: archive directory must exist: {path}".format(
            prog=os.path.basename(sys.argv[0]),
            path=ctx.archive_dir,
        ))

    read_config(ctx)

    beanstalk = connect(ctx)
    beanstalk.watch(ctx.tube)
    beanstalk.ignore('default')

    while True:
        if need_restart():
            restart()

        job = beanstalk.reserve(timeout=60)
        if job is None:
            continue

        # bury the job so it won't be re-run if it fails
        job.bury()
        log.debug('Reserved job %d', job.jid)
        log.debug('Config is: %s', job.body)
        job_config = yaml.safe_load(job.body)

        job_config['job_id'] = str(job.jid)
        safe_archive = safepath.munge(job_config['name'])
        archive_path_full = os.path.join(
            ctx.archive_dir, safe_archive, str(job.jid))
        job_config['archive_path'] = archive_path_full

        # If the teuthology branch was not specified, default to master and
        # store that value.
        teuthology_branch = job_config.get('teuthology_branch', 'master')
        job_config['teuthology_branch'] = teuthology_branch

        teuth_path = os.path.join(os.getenv("HOME"),
                                  'teuthology-' + teuthology_branch)

        fetch_teuthology_branch(path=teuth_path, branch=teuthology_branch)

        teuth_bin_path = os.path.join(teuth_path, 'virtualenv', 'bin')
        if not os.path.isdir(teuth_bin_path):
            raise RuntimeError("teuthology branch %s at %s not bootstrapped!" %
                               (teuthology_branch, teuth_bin_path))

        if job_config.get('last_in_suite'):
            log.debug('Generating coverage for %s', job_config['name'])
            args = [
                os.path.join(teuth_bin_path, 'teuthology-results'),
                '--timeout',
                str(job_config.get('results_timeout', 21600)),
                '--email',
                job_config['email'],
                '--archive-dir',
                os.path.join(ctx.archive_dir, safe_archive),
                '--name',
                job_config['name'],
            ]
            subprocess.Popen(args=args)
        else:
            log.debug('Creating archive dir...')
            safepath.makedirs(ctx.archive_dir, safe_archive)
            log.info('Running job %d', job.jid)
            run_job(job_config, teuth_bin_path)
        job.delete()


def run_with_watchdog(process, job_config):
    # Only push the information that's relevant to the watchdog, to save db
    # load
    job_info = dict(
        name=job_config['name'],
        job_id=job_config['job_id'],
    )

    while process.poll() is None:
        report.try_push_job_info(job_info, dict(status='running'))
        time.sleep(teuth_config.watchdog_interval)

    # The job finished. Let's make sure paddles knows.
    branches_with_reporting = ('master', 'next', 'last')
    if job_config.get('teuthology_branch') not in branches_with_reporting:
        # The job ran with a teuthology branch that may not have the reporting
        # feature. Let's call teuthology-report (which will be from the master
        # branch) to report the job manually.
        args = ['teuthology-report',
                '-r',
                job_info['name'],
                '-j',
                job_info['job_id'],
                ]
        subprocess.Popen(args).wait()
    else:
        # Let's make sure that paddles knows the job is finished. We don't know
        # the status, but if it was a pass or fail it will have already been
        # reported to paddles. In that case paddles ignores the 'dead' status.
        # If the job was killed, paddles will use the 'dead' status.
        report.try_push_job_info(job_info, dict(status='dead'))


def run_job(job_config, teuth_bin_path):
    arg = [
        os.path.join(teuth_bin_path, 'teuthology'),
    ]
    # The following is for compatibility with older schedulers, from before we
    # started merging the contents of job_config['config'] into job_config
    # itself.
    if 'config' in job_config:
        inner_config = job_config.pop('config')
        if not isinstance(inner_config, dict):
            log.debug("run_job: job_config['config'] isn't a dict, it's a %s",
                      str(type(inner_config)))
        else:
            job_config.update(inner_config)

    if job_config['verbose']:
        arg.append('-v')

    arg.extend([
        '--lock',
        '--block',
        '--owner', job_config['owner'],
        '--archive', job_config['archive_path'],
        '--name', job_config['name'],
    ])
    if job_config['description'] is not None:
        arg.extend(['--description', job_config['description']])
    arg.append('--')

    with tempfile.NamedTemporaryFile(prefix='teuthology-worker.',
                                     suffix='.tmp',) as tmp:
        yaml.safe_dump(data=job_config, stream=tmp)
        tmp.flush()
        arg.append(tmp.name)
        p = subprocess.Popen(args=arg)
        log.info("Job archive: %s", job_config['archive_path'])

        if teuth_config.results_server:
            log.info("Running with watchdog")
            run_with_watchdog(p, job_config)
        else:
            log.info("Running without watchdog")
            p.wait()

        if p.returncode != 0:
            log.error('Child exited with code %d', p.returncode)
        else:
            log.info('Success!')
