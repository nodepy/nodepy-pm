# Copyright (c) 2017 Niklas Rosenstein
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import print_function
from nodepy.utils import json
from operator import itemgetter
from six.moves import input

import argparse
import collections
import functools
import getpass
import nodepy
import os
import pip.req
import six
import sys
import textwrap

import manifest from './lib/manifest'
import semver from './lib/semver'
import refstring from './lib/refstring'
import logger from './lib/logger'
import _install from './lib/install'
import {RegistryClient} from './lib/registry'
import PackageLifecycle from './lib/package-lifecycle'
import env, {PACKAGE_MANIFEST} from './lib/env'


def fatal(*message, **kwargs):
  """
  Prints *message* to stderr and
  """

  code = kwargs.pop('code', 1)
  kwargs.setdefault('file', sys.stderr)
  print('fatal:', *message, **kwargs)
  sys.exit(code)


def read_gitref():
  """
  Reads the `.gitref` file that is created in `scripts/pre-install.py`.
  If the file does not exist, `<unknown_gitref>` is returned.
  """

  gitref = module.directory.joinpath('.gitref')
  if gitref.is_file():
    with gitref.open('r') as fp:
      return fp.read().strip()
  return '<unknown_gitref>'


def reindent(text, indent):
  """
  Dedents *text* and then re-indents it with the specified *indent*. The
  *indent* parameter must be a string.
  """

  lines = textwrap.dedent(text).split('\n')
  while lines and not lines[0].strip():
    lines.pop(0)
  while lines and not lines[-1].strip():
    lines.pop()
  return indent + ('\n' + indent).join(lines)


def get_install_location(global_, root):
  """
  Returns the package install location name depending on whether the *global_*
  or *root* flag was specified. If both are specified, #fatal() is used to
  print an error message.

  Returns:
    str: Either `'local'`, `'global'` or `'root'`.
  """

  if global_ and root:
    fatal('-g,--global and --root can not be used together')
  elif global_:
    if env.is_virtualenv():
      print('Note: detected virtual environment, upgrading -g,--global to --root')
      return 'root'
    return 'global'
  elif root:
    return 'root'
  else:
    return 'local'


def create_installer(args):
  """
  Creates an #_install.Installer from the command-line arguments *args*.
  This function is used in the #install() function.
  """

  location = get_install_location(args.global_, args.root)
  installer = _install.Installer(
    upgrade=args.upgrade,
    install_location=get_install_location(args.global_, args.root),
    pip_separate_process=args.pip_separate_process,
    pip_use_target_option=args.pip_use_target_option,
    recursive=args.recursive,
    verbose=args.verbose
  )
  installer.ignore_installed = args.pip_ignore_installed
  return installer


def load_manifest(filename):
  """
  Loads a manifest and reports issues after it is validated.
  """

  data = manifest.load(filename)
  for field in manifest.validate(data):
    name = field.cfg or ''
    if name and name[-1] != '.':
      name += '>'
    name += field.name
    for msg in field.warnings:
      print('WARNING: {}@{} {}'.format(filename, name, msg))
    for msg in field.errors:
      print('CRITICAL: {}@{} {}'.format(filename, name, msg))
  return data


__version__ = "nodepy {} ({})".format(
  module.package.payload['version'], read_gitref())


parser = argparse.ArgumentParser(description=reindent('''
    Nodepy-pm is the package manager for the Node.py CPython runtime.
    It can be used to install standard Python and Node.py packages locally,
    globally (per-user) or system-wide. Node.py does not require virtualenvs
    to ensure a locally unique package environment.

    The following formats can be used to install Node.py packages:

      [@<scope>/]<package>[@<version>]
      git+<url>
      <archive>.tar[.<compression>]
      <package_directory>

    The following formats can be used to install packages via Pip:

      ~<pipspec>
      pip+<pipspec>
  ''', '  '),
  formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument('--version', action='version',
  version='{} on Python {}'.format(__version__, sys.version.split(' ', 1)[0]),
  help='Report the Node.py version and exit.')

subparsers = parser.add_subparsers(dest='cmd')

init_parser = subparsers.add_parser('init')
init_parser.add_argument('directory', nargs='?',
  help='The directory to save the nodepy.json to.')

install_parser = subparsers.add_parser('install')
install_parser.add_argument('packages', metavar='SPEC', nargs='*',
  help='A list of one or more package specifiers to install. If no packages '
    'are specified (together with -e, --develop), the dependencies of the '
    'current package are installed (--upgrade will be implied in that case).')
install_parser.add_argument('-U', '--upgrade', action='store_true',
  help='Don\'t skip installing packages that already exist but instead '
    'install the upgrade to the version.')
install_parser.add_argument('--pip', nargs='...', default=[],
  help='Consider all following arguments to be Pip requirements.')
install_parser.add_argument('-e', '--develop', action='append', metavar='PATH',
  default=[],
  help='Install a Node.py package in development mode. This will create a '
    'plain-test link file instead of installing the package contents to the '
    'modules directory. This only works for packages existing on the '
    'filesystem.')
install_parser.add_argument('-g', '--global', dest='global_', action='store_true',
  help='Install packages globally (per-user). Node.py packages will be '
    'installed near site-packages/ under a nodepy-modules/ directory. Pip '
    'packages will be installed as `pip install --user`. This option implies '
    'the --internal option for Node.py packages.')
install_parser.add_argument('--root', action='store_true',
  help='Install packages system-wide. Node.py pakcages will be installed '
    'near site-packages/ under a nodepy-modules/ directory. Pip packages '
    'will be installed as `pip install`. This option implies the --internal '
    'option for Node.py packages.')
install_parser.add_argument('--system', action='store_true',
  help='Alias for the --root option.')
install_parser.add_argument('-PI', '--pip-ignore-installed',
  action='store_true', help='Pass the --ignore-installed option to Pip.')
install_parser.add_argument('-PS', '--pip-separate-process',
  action='store_true', help='Install Pip packages using a separate Pip '
    'process. Try this when Pip installations fails.')
install_parser.add_argument('-PT', '--pip-use-target-option',
  action='store_true', help='Use Pip\'s --target option instead of --prefix. '
    'Note that Pip will not install scripts with --target. Try this when '
    'Pip installations fail.')
install_parser.add_argument('--packagedir',
  help='The package directory. Defaults to the current working directory. '
    'Used to install the current package\'s (dev-)dependencies and to save '
    'package information when using the --save, --save-dev or --save-ext '
    'options.')
install_parser.add_argument('-R', '--recursive', action='store_true',
  help='Ensure that dependencies are satisfied recursively. This can be '
    'used when packages where uninstall that may still be required by '
    'other packages. Attempts to dependencies of already satisfied '
    'dependencies.')
install_parser.add_argument('--dev', action='store_true',
  help='Install development dependencies or not. By default, development '
    'dependencies are only installed for the current package.')
install_parser.add_argument('--production', action='store_true',
  help='Do not install development dependencies.')
install_parser.add_argument('--save', action='store_true',
  help='Add the installed packages as dependencies to the current project. '
    'Requires a nodepy.json manifest in the current working directory or the '
    'directory specified with --packagedir.')
install_parser.add_argument('--save-dev', action='store_true',
  help='Add the installed packages as development dependencies.')
install_parser.add_argument('--save-ext', action='store_true',
  help='Add the installed Node.py packages to the "extensions" field. '
    'This option implies --save.')
install_parser.add_argument('-v', '--verbose', action='count',
  help='Enable verbose output for nodepy-pm and Pip.')
install_parser.add_argument('--internal', action='store_true', default=None,
  help='Install the specified Node.py packages as internal dependencies. '
    'This flag has no immediate effect on local install, but the --internal '
    'flag will be added when using --save or --save-dev, causing the '
    'dependencies of your package to be installed for your package only.')
install_parser.add_argument('--no-internal', action='store_true',
  help='Use this flag to disable the implicit --internal flag on --root '
    'and --global installations.')
install_parser.add_argument('--pure', action='store_true',
  help='Install Node.py packages without their command-line scripts.')

uninstall_parser = subparsers.add_parser('uninstall')
uninstall_parser.add_argument('packages', nargs='+',
  help='Full names of packages to uninstall.')
uninstall_parser.add_argument('-g', '--global', dest='global_', action='store_true',
  help='Uninstall the package(s) from the global package directory.')
uninstall_parser.add_argument('--root', action='store_true',
  help='Uninstall the package(s) from the system-wide package directory.')
uninstall_parser.add_argument('--system', action='store_true',
  help='Alias for --root.')

dist_parser = subparsers.add_parser('dist')

bin_parser = subparsers.add_parser('bin')
bin_parser.add_argument('-g', '--global', dest='global_', action='store_true')
bin_parser.add_argument('--root', action='store_true')
bin_parser.add_argument('--system', action='store_true')
bin_parser.add_argument('--pip', action='store_true')

dirs_parser = subparsers.add_parser('dirs')
dirs_parser.add_argument('-g', '--global', dest='global_', action='store_true')
dirs_parser.add_argument('--root', action='store_true')
dirs_parser.add_argument('--system', action='store_true')
dirs_parser.add_argument('--bin', action='store_true')
dirs_parser.add_argument('--packages', action='store_true')
dirs_parser.add_argument('--pip-prefix', action='store_true')
dirs_parser.add_argument('--pip-bin', action='store_true')
dirs_parser.add_argument('--pip-lib', action='store_true')

run_parser = subparsers.add_parser('run')
run_parser.add_argument('script',
  help='The name of the script or program to run. Scripts executed with '
    'this command have the .nodepy/bin directory in their PATH.')
run_parser.add_argument('argv', nargs=argparse.REMAINDER)


def main(argv=None):
  args = parser.parse_args(argv)
  if args.cmd:
    return globals()['do_' + args.cmd](args)
  else:
    parser.print_help()
    return 0


def do_install(args):
  if not args.packagedir:
    args.packagedir = '.'
  manifest_filename = os.path.join(args.packagedir, PACKAGE_MANIFEST)

  args.root = args.root or args.system

  # --save and --save-dev are incompatible with each other.
  if args.save and args.save_dev:
    install_parser.error('incompatible flags --save and --save-dev')
    return 1

  # Can't have both --dev and --production.
  if args.dev and args.production:
    install_parser.error('incompatible flags --dev and --production')

  # --save-ext should not be combined with --save-dev. Imply --save otherwise.
  if args.save_ext:
    if args.save_dev:
      print('warning: --save-ext should not be combined with --save-dev.')
      print('         Extensions must be available during runtime.')
    else:
      save = True

  # Imply --internal with --root or --global, unless --no-internal is passed.
  if (args.global_ or args.root) and not args.internal and not args.no_internal:
    args.internal = True
    flag = ('--global' if args.global_ else '--root')
    print('Note: implying --internal due to {}.'.format(flag))

  # Read in the manifest, if it exists.
  manifest_data = None
  if os.path.isfile(manifest_filename):
    manifest_data = load_manifest(manifest_filename)

  # Can't do any saving when there's no manifest.
  if (args.save or args.save_dev or args.save_ext) and manifest_data is None:
    fatal('can not --save, --save-dev or --save-ext without nodepy.json')
    return 1

  pure_install = (not args.packages and not args.develop and not args.pip)

  # Default to --dev if no packages are specified.
  if (not args.dev and not args.production):
    args.dev = pure_install
    args.production = not args.dev

  installer = create_installer(args)

  # If no packages to install are specified, install the dependencies of the
  # current packages. Imply --upgrade and --develop.
  if pure_install:
    installer.upgrade = True
    success, _manifest = installer.install_from_directory(
        '.', develop=True, dev=args.dev)
    if not success:
      return 1
    installer.relink_pip_scripts()
    return 0

  # Parse the requirements from the command-line.
  pip_packages = []
  npy_packages = []
  def handle_spec(spec, develop):
    if spec.startswith('~'):
      pip_packages.append(manifest.PipRequirement.from_line(spec[1:]))
    elif spec.startswith('pip+'):
      pip_packages.append(manifest.PipRequirement.from_line(spec[4:]))
    else:
      req = manifest.Requirement.from_line(spec, expect_name=True)
      req.inherit_values(link=develop, internal=args.internal, pure=args.pure)
      npy_packages.append(req)
  for pkg in args.packages:
    handle_spec(pkg, False)
  for pkg in args.develop:
    handle_spec(pkg, True)
  for pkg in args.pip:
    handle_spec('pip+' + pkg, False)

  # Install Python dependencies.
  python_deps = {}
  python_additional = []
  for spec in pip_packages:
    if spec.link:
      python_additional.append(str(spec.link))
    elif (args.save or args.save_dev) and not spec.req:
      fatal("'{}' is not something we can install via nodepy-pm with --save/--save-dev".format(spec.spec))
    if spec.req:
      python_deps[spec.name] = str(spec.specifier)
    else:
      python_additional.append(str(spec))
  if (python_deps or python_additional):
    if not installer.install_python_dependencies(python_deps, args=python_additional):
      fatal('installation failed')

  # Install Node.py dependencies.
  req_names = {}
  for req in npy_packages:
    success, info = installer.install_from_requirement(req)
    if not success:
      fatal('installation failed')
    if req.name:
      assert info[0] == pkg.name, (info, pkg)
    req_names[req] = info[0]
    if req.type == 'registry':
      req.selector = semver.Selector('~' + str(info[1]))

  installer.relink_pip_scripts()

  # Insert extensions.
  if args.save_ext and npy_packages:
    print('Saving extensions:')
    extensions = manifest_data.setdefault('extensions', [])
    for req_name in sorted(req_names.values()):
      if req_name not in extensions:
        extensions.append(req_name)

  # Choose the keys that the dependencies will be saved to.
  if args.save_dev:
    if 'cfg(dev)' in manifest_data:
      deps = lambda: manifest_data['cfg(dev)'].setdefault('dependencies', {})
      pip_deps = lambda: manifest_data['cfg(dev)'].setdefault('pip_dependencies', {})
    else:
      deps = lambda: manifest_data.setdefault('cfg(dev).dependencies', {})
      pip_deps = lambda: manifest_data.setdefault('cfg(dev).pip_dependencies', {})
  elif args.save:
    deps = lambda: manifest_data.setdefault('dependencies', {})
    pip_deps = lambda: manifest_data.setdefault('pip_dependencies', {})

  # Insert Node.py packages into the manifest.
  if (args.save or args.save_dev) and npy_packages:
    print('Saving dependencies:')
    for req in npy_packages:
      deps()[req_names[req]] = str(req)
      print("  {}: {}".format(req_names[req], str(req)))

  # Insert Python packages into the manifest.
  if (args.save or args.save_dev) and python_deps:
    print('Saving Pip dependencies:')
    for pkg_name, dist_info in installer.installed_python_libs.items():
      if not dist_info:
        print('warning: could not find .dist-info of module "{}"'.format(pkg_name))
        pip_deps()[pkg_name] = ''
        print('  "{}": ""'.format(pkg_name))
      else:
        pip_deps()[dist_info['name']] = '>=' + dist_info['version']
        print('  "{}": "{}"'.format(dist_info['name'], dist_info['version']))

  # Write the changes to the manifest.
  if (args.save or args.save_dev or args.save_ext) and (npy_packages or python_deps):
    with open(manifest_filename, 'w') as fp:
      json.dump(manifest_data, fp, indent=2)

  print()


def do_uninstall(args):
  packages = []
  for pkg in args.packages:
    if pkg == '.' or os.path.exists(pkg):
      filename = os.path.join(pkg, PACKAGE_MANIFEST)
      manifest = load_manifest(filename)
      pkg = manifest['name']
    packages.append(pkg)

  location = get_install_location(args.global_, args.root)
  installer = _install.Installer(install_location=location)
  for pkg in packages:
    installer.uninstall(pkg)


def do_dist(args):
  PackageLifecycle().dist()


def do_init(args):
  filename = os.path.join(args.directory or '.', PACKAGE_MANIFEST)
  if os.path.isfile(filename):
    print('error: "{}" already exists'.format(filename))
    return 1

  questions = [
    ('Package Name', 'name', None),
    ('Package Version', 'version', '1.0.0'),
    ('?Description', 'description', None),
    ('?Author E-Mail(s)', 'authors', None),
    ('?License', 'license', 'MIT')
  ]

  results = collections.OrderedDict()
  for qu in questions:
    msg = qu[0]
    opt = msg.startswith('?')
    if opt: msg = msg[1:]
    if qu[2]:
      msg += ' [{}]'.format(qu[2])
    while True:
      reply = input(msg + '? ').strip() or qu[2]
      if reply or opt: break
    if reply and reply != '-':
      results[qu[1]] = reply

  if 'author' in results:
    results['authors'] = [results.pop('author')]

  print('This is your new nodepy.json:')
  print()
  result = json.dumps(results, indent=2)
  print(result)
  print()
  reply = input('Are you okay with this? [Y/n] ').strip().lower()
  if reply not in ('', 'y', 'yes', 'ok'):
    return

  with open(filename, 'w') as fp:
    fp.write(result)


def do_bin(args):
  location = get_install_location(args.global_, args.root)
  dirs = env.get_directories(location)
  if args.pip:
    print(dirs['pip_bin'])
  else:
    print(dirs['bin'])


def do_dirs(args):
  location = get_install_location(args.global_, args.root)
  dirs = env.get_directories(location)
  if args.packages:
    print(dirs['packages'])
  elif args.bin:
    print(dirs['bin'])
  elif args.pip_prefix:
    print(dirs['pip_prefix'])
  elif args.pip_bin:
    print(dirs['pip_bin'])
  elif args.pip_lib:
    print(dirs['pip_lib'])
  else:
    print('Packages:\t', dirs['packages'])
    print('Bin:\t\t', dirs['bin'])
    print('Pip Prefix:\t', dirs['pip_prefix'])
    print('Pip Bin:\t', dirs['pip_bin'])
    print('Pip Lib:\t', dirs['pip_lib'])


def do_run(args):
  if not PackageLifecycle(allow_no_manifest=True).run(args.script, args.argv):
    fatal("no script '{}'".format(args.script))


if require.main == module:
  sys.exit(main())
