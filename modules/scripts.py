# Copyright (C) 2013 Kristoffer Gronlund <kgronlund@suse.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation; either
# version 2 of the License, or (at your option) any later version.
#
# This software is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
#

import config
import yaml
import time
import random
import os
import subprocess
import shutil
from msg import err_buf
import userdir

try:
    from psshlib import api as pssh
    has_pssh = True
except ImportError:
    has_pssh = False

import utils

try:
    import json
except ImportError:
    import simplejson as json


_SCRIPTS_DIR = [os.path.join(userdir.CONFIG_HOME, 'scripts'),
                os.path.join(config.path.sharedir, 'scripts')]


def _check_control_persist():
    '''
    Checks if ControlPersist is available. If so,
    we'll use it to make things faster.
    '''
    cmd = subprocess.Popen('ssh -o ControlPersist'.split(),
                           stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    (out, err) = cmd.communicate()
    return "Bad configuration option" not in err


def _generate_workdir_name():
    '''
    Generate a temporary folder name to use while
    running the script
    '''
    # TODO: make use of /tmp configurable
    basefile = 'crm-tmp-%s-%s' % (time.time(), random.randint(0, 2**48))
    basetmp = os.path.join(utils.get_tempdir(), basefile)
    return basetmp


def resolve_script(name):
    for d in _SCRIPTS_DIR:
        script_main = os.path.join(d, name, 'main.yml')
        if os.path.isfile(script_main):
            return script_main
    return None


def list_scripts():
    '''
    List the available cluster installation scripts.
    '''
    l = []

    def path_combine(p0, p1):
        if p0:
            return os.path.join(p0, p1)
        return p1

    def recurse(root, prefix):
        try:
            curdir = path_combine(root, prefix)
            for f in os.listdir(curdir):
                if os.path.isdir(os.path.join(curdir, f)):
                    if os.path.isfile(os.path.join(curdir, f, 'main.yml')):
                        l.append(path_combine(prefix, f))
                    else:
                        recurse(root, path_combine(prefix, f))
        except OSError:
            pass
    for d in _SCRIPTS_DIR:
        recurse(d, '')
    return sorted(l)


def load_script(script):
    main = resolve_script(script)
    if main and os.path.isfile(main):
        return yaml.load(open(main))[0]
    return None


def _step_action(step):
    name = step.get('name')
    if 'type' in step:
        return name, step.get('type'), step.get('call')
    else:
        for typ in ['collect', 'validate', 'apply', 'apply_local', 'report']:
            if typ in step:
                return name, typ, step[typ].strip()
    return name, None, None


def arg0(cmd):
    return cmd.split()[0]


def _verify_step(scriptdir, scriptname, step):
    step_name, step_type, step_call = _step_action(step)
    if not step_name:
        raise ValueError("Error in %s: Step missing name" % (scriptname))
    if not step_type:
        raise ValueError("Error in %s: Step '%s' has no action defined" %
                         (scriptname, step_name))
    if not step_call:
        raise ValueError("Error in %s: Step '%s' has no call defined" %
                         (scriptname, step_name))
    if not os.path.isfile(os.path.join(scriptdir, arg0(step_call))):
        raise ValueError("Error in %s: Step '%s' file not found: %s" %
                         (scriptname, step_name, step_call))


def verify(name):
    script = resolve_script(name)
    if not script:
        raise ValueError("%s not found" % (name))
    script_dir = os.path.dirname(script)
    main = load_script(name)
    for key in ['name', 'description', 'parameters', 'steps']:
        if key not in main:
            raise ValueError("Error in %s: Missing %s" % (name, key))
    for step in main.get('steps', []):
        _verify_step(script_dir, name, step)
    return main

COMMON_PARAMS = [('nodes', None, 'List of nodes to execute the script for'),
                 ('dry_run', 'no', 'If set, only execute collecting and validating steps'),
                 ('step', None, 'If set, only execute the named step'),
                 ('statefile', None, 'When single-stepping, the state is saved in the given file')]


def describe(name):
    '''
    Prints information about the given script.
    '''
    script = load_script(name)
    from help import HelpEntry

    def rewrap(txt):
        import textwrap
        paras = []
        for para in txt.split('\n'):
            paras.append('\n'.join(textwrap.wrap(para)))
        return '\n\n'.join(paras)
    desc = rewrap(script.get('description', 'No description available'))

    params = script.get('parameters', [])
    desc += "Parameters (* = Required):\n"
    for name, value, description in COMMON_PARAMS:
        if value is not None:
            defval = ' (default: %s)' % (value)
        else:
            defval = ''
        desc += "  %-24s %s%s\n" % (name, description, defval)
    for p in params:
        rq = ''
        if p.get('required'):
            rq = '*'
        defval = p.get('default', None)
        if defval is not None:
            defval = ' (default: %s)' % (defval)
        else:
            defval = ''
        desc += "  %-24s %s%s\n" % (p['name'] + rq, p.get('description', ''), defval)

    desc += "\nSteps:\n"
    for step in script.get('steps', []):
        name = step.get('name')
        if name:
            desc += "  * %s\n" % (name)

    e = HelpEntry(script.get('name', name), desc)
    e.paginate()


def _make_options():
    "Setup pssh options. TODO: Allow setting user/port/timeout"
    opts = pssh.Options()
    opts.timeout = 60
    opts.recursive = True
    opts.ssh_options += [
        'PasswordAuthentication=no',
        #'StrictHostKeyChecking=no',
        'ControlPersist=no']
    return opts


def _open_script(name):
    filename = resolve_script(name)
    main = verify(name)
    if main is None or filename is None:
        raise ValueError('Loading script failed: ' + name)
    script_dir = os.path.dirname(filename)
    return main, filename, script_dir


def _parse_parameters(name, args, main):
    '''
    Parse run parameters into a dict.
    Also extract parameters to the script
    runner: hosts, dry_run, step etc.
    '''
    args = utils.nvpairs2dict(args)
    params = {}
    for key, val in args.iteritems():
        params[key] = val
    for param in main['parameters']:
        name = param['name']
        if name not in params:
            if 'default' in param:
                params[name] = param['default']
            else:
                raise ValueError("Missing required parameter %s" % (name))
    hosts = args.get('nodes')
    if hosts is None:
        hosts = utils.list_cluster_nodes()
    else:
        hosts = hosts.replace(',', ' ').split()
    if not hosts:
        raise ValueError("No hosts")
    dry_run = args.get('dry_run', False)
    step = args.get('step', None)
    statefile = args.get('statefile', None)
    return params, hosts, dry_run, step, statefile


def _extract_localnode(hosts):
    """
    Remove loal node from hosts list, so
    we can treat it separately
    """
    local_node = utils.this_node()
    if local_node in hosts:
        hosts.remove(local_node)
    else:
        local_node = None
    return local_node, hosts


def _set_controlpersist(opts):
    #_has_controlpersist = _check_control_persist()
    #if _has_controlpersist:
    #    opts.ssh_options += ["ControlMaster=auto",
    #                         "ControlPersist=30s",
    #                         "ControlPath=/tmp/crm-ssh-%r@%h:%p"]
    # unfortunately, due to bad interaction between pssh and ssh,
    # ControlPersist is broken
    # See: http://code.google.com/p/parallel-ssh/issues/detail?id=67
    # Fixed in openssh 6.3
    pass


def _create_script_workdir(scriptdir, workdir):
    "Create workdir and copy contents of scriptdir into it"
    if subprocess.call(["mkdir", "-p", os.path.dirname(workdir)], shell=False) != 0:
        raise ValueError("Failed to create temporary working directory")
    try:
        shutil.copytree(scriptdir, workdir)
    except (IOError, OSError), e:
        raise ValueError(e)


def _copy_utils(dst):
    '''
    Copy run utils to the destination directory
    '''
    try:
        import glob
        for f in glob.glob(os.path.join(config.path.sharedir, 'utils/*.py')):
            shutil.copy(os.path.join(config.path.sharedir, f), dst)
    except (IOError, OSError), e:
        raise ValueError(e)


def _create_remote_workdirs(hosts, path, opts):
    "Create workdirs on remote hosts"
    ok = True
    for host, result in pssh.call(hosts,
                                  "mkdir -p %s" % (os.path.dirname(path)),
                                  opts).iteritems():
        if isinstance(result, pssh.Error):
            err_buf.error("[%s]: %s" % (host, result))
            ok = False
    if not ok:
        raise ValueError("Failed to create working folders, aborting.")


def _copy_to_remote_dirs(hosts, path, opts):
    "Copy a local folder to same location on remote hosts"
    ok = True
    for host, result in pssh.copy(hosts,
                                  path,
                                  path, opts).iteritems():
        if isinstance(result, pssh.Error):
            err_buf.error("[%s]: %s" % (host, result))
            ok = False
    if not ok:
        raise ValueError("Failed when copying script data, aborting.")


def _copy_to_all(workdir, hosts, local_node, src, dst, opts):
    """
    Copy src to dst both locally and remotely
    """
    ok = True
    ret = pssh.copy(hosts, src, dst, opts)
    for host, result in ret.iteritems():
        if isinstance(result, pssh.Error):
            err_buf.error("[%s]: %s" % (host, result))
            ok = False
        else:
            rc, out, err = result
            if rc != 0:
                err_buf.error("[%s]: %s" % (host, err))
                ok = False
    if local_node and not src.startswith(workdir):
        try:
            if os.path.abspath(src) != os.path.abspath(dst):
                if os.path.isfile(src):
                    shutil.copy(src, dst)
                else:
                    shutil.copytree(src, dst)
        except (IOError, OSError, shutil.Error), e:
            err_buf.error("[%s]: %s" % (utils.this_node(), e))
            ok = False
    return ok


class RunStep(object):
    def __init__(self, main, params, local_node, hosts, opts, dry_run, workdir):
        self.main = main
        self.data = [params]
        self.local_node = local_node
        self.hosts = hosts
        self.opts = opts
        self.dry_run = dry_run
        self.workdir = workdir
        self.statefile = os.path.join(self.workdir, 'script.input')
        self.dstfile = os.path.join(self.workdir, 'script.input')

    def _build_cmdline(self, sname, stype, scall):
        cmdline = 'cd "%s"; ./%s' % (self.workdir, scall)
        if config.core.debug:
            import pprint
            print "step:", sname, stype, scall
            print "cmdline:", cmdline
            print "data:"
            pprint.pprint(self.data)
        return cmdline

    def single_step(self, step_name, statefile):
        self.statefile = statefile
        for step in self.main['steps']:
            name, action, call = _step_action(step)
            if name == step_name:
                # if this is not the first step, load step data
                if step != self.main['steps'][0]:
                    if os.path.isfile(statefile):
                        self.data = json.load(open(statefile))
                    else:
                        raise ValueError("No state for step: %s" % (step_name))
                result = self.run_step(name, action, call)
                json.dump(self.data, open(self.statefile, 'w'))
                return result
        err_buf.error("%s: Step not found" % (step_name))
        return False

    def _update_state(self):
        json.dump(self.data, open(self.statefile, 'w'))
        return _copy_to_all(self.workdir,
                            self.hosts,
                            self.local_node,
                            self.statefile,
                            self.dstfile,
                            self.opts)

    def run_step(self, name, action, call):
        """
        Execute a single step
        """
        cmdline = self._build_cmdline(name, action, call)

        if not self._update_state():
            raise ValueError("Failed when updating input, aborting.")

        ok = False
        if action in ('collect', 'apply'):
            result = self._process_remote(cmdline)
            if result is not None:
                self.data.append(result)
                ok = True
        elif action == 'validate':
            result = self._process_local(cmdline)
            if result is not None:
                if result:
                    result = json.loads(result)
                else:
                    result = {}
                self.data.append(result)
                if isinstance(result, dict):
                    for k, v in result.iteritems():
                        self.data[0][k] = v
                ok = True
        elif action == 'apply_local':
            result = self._process_local(cmdline)
            if result is not None:
                if result:
                    result = json.loads(result)
                else:
                    result = {}
                self.data.append(result)
                ok = True
        elif action == 'report':
            result = self._process_local(cmdline)
            if result is not None:
                err_buf.ok(name)
                print result
                return True
        if ok:
            err_buf.ok(name)
        return ok

    def all_steps(self):
        # TODO: run asynchronously on remote nodes
        # run on remote nodes
        # run on local nodes
        # TODO: wait for remote results
        for step in self.main['steps']:
            name, action, call = _step_action(step)
            if self.dry_run and action in ('apply', 'apply_local'):
                break
            if not self.run_step(name, action, call):
                return False
        return True

    def _process_remote(self, cmdline):
        """
        Handle a step that executes on all nodes
        """
        ok = True
        step_result = {}
        for host, result in pssh.call(self.hosts,
                                      cmdline,
                                      self.opts).iteritems():
            if isinstance(result, pssh.Error):
                err_buf.error("[%s]: %s" % (host, result))
                ok = False
            else:
                rc, out, err = result
                if rc != 0:
                    err_buf.error("[%s]: %s%s" % (host, out, err))
                    ok = False
                else:
                    step_result[host] = json.loads(out)
        if self.local_node:
            rc, out, err = utils.get_stdout_stderr(cmdline)
            if rc != 0:
                err_buf.error("[%s]: %s" % (self.local_node, err))
                ok = False
            else:
                step_result[self.local_node] = json.loads(out)
        if ok:
            return step_result
        return None

    def _process_local(self, cmdline):
        """
        Handle a step that executes locally
        """
        rc, out, err = utils.get_stdout_stderr(cmdline)
        if rc != 0:
            err_buf.error("[%s]: Error (%d): %s" % (self.local_node, rc, err))
            return None
        return out


def _run_cleanup(hosts, workdir, opts):
    "Clean up after the cluster script"
    if hosts and workdir:
        for host, result in pssh.call(hosts,
                                      "%s %s" % (os.path.join(workdir, 'crm_clean.py'),
                                                 workdir),
                                      opts).iteritems():
            if isinstance(result, pssh.Error):
                err_buf.warning("[%s]: Failed to clean up %s" % (host, workdir))
    if workdir and os.path.isdir(workdir):
        shutil.rmtree(workdir)


def run(name, args):
    '''
    Run the given script on the given set of hosts
    name: a cluster script is a folder <name> containing a main.yml file
    args: list of nvpairs
    '''
    if not has_pssh:
        raise ValueError("PSSH library is not installed or is not up to date.")
    # TODO: allow more options here, like user, port...
    opts = _make_options()
    hosts = None
    workdir = _generate_workdir_name()
    try:
        main, filename, script_dir = _open_script(name)
        params, hosts, dry_run, step, statefile = _parse_parameters(name, args, main)
        err_buf.info(main['name'])
        err_buf.info("Nodes: " + ', '.join(hosts))
        local_node, hosts = _extract_localnode(hosts)
        _set_controlpersist(opts)
        _create_script_workdir(script_dir, workdir)
        _copy_utils(workdir)
        _create_remote_workdirs(hosts, workdir, opts)
        _copy_to_remote_dirs(hosts, workdir, opts)
        # make sure all path references are relative to the script directory
        if statefile:
            statefile = os.path.abspath(statefile)
        os.chdir(workdir)

        stepper = RunStep(main, params, local_node, hosts, opts, dry_run, workdir)

        if step or statefile:
            if not step or not statefile:
                raise ValueError("Must set both step and statefile")
            return stepper.single_step(step, statefile)
        else:
            return stepper.all_steps()

    except (OSError, IOError), e:
        import traceback
        traceback.print_exc()
        raise ValueError("Internal error while running %s: %s" % (name, e))
    finally:
        _run_cleanup(hosts, workdir, opts)