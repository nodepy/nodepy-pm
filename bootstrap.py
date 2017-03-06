# Copyright (c) 2017  Niklas Rosenstein
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
"""
Bootstrap the installation of the PPYM, installing all Python dependencies
into `nodepy_modules/.pip`.
"""

if require.main != module:
  raise RuntimeError('bootstrap must not be required')

import click
import json
import os
import pip.commands
import shutil
import sys


@click.command()
@click.option('--bootstrap/--no-bootstrap', default=True, help="Don't bootstrap Python dependencies")
@click.option('-g', '--global', 'global_', is_flag=True, help="Install PPYM globally.")
@click.option('--root', is_flag=True, help="Install PPYM in the Python root folder.")
@click.option('-U', '--upgrade', is_flag=True, help="Uninstall previous versions instead of skipping the new version.")
@click.option('--develop', is_flag=True, help="If --install, install in development mode.")
def main(bootstrap, global_, root, upgrade, develop):
  """
  Bootstrap the PPYM installation.
  """

  existed_before = os.path.isdir('nodepy_modules')

  if bootstrap:
    print("Bootstrapping PPYM dependencies with Pip ...")
    with open(os.path.join(__directory__, 'package.json')) as fp:
      package = json.load(fp)

    cmd = ['--prefix', 'nodepy_modules/.pip']
    for key, value in package['python-dependencies'].items():
      cmd.append(key + value)

    res = pip.commands.InstallCommand().main(cmd)
    if res != 0:
      print('error: Pip installation failed')
      sys.exit(res)

  # This is necessary on Python 2.7 (and possibly other versions) as
  # otherwise importing the newly installed Python modules will fail.
  sys.path_importer_cache.clear()

  cmd = ['install']
  if upgrade:
    cmd.append('--upgrade')
  if global_:
    cmd.append('--global')
  if root:
    cmd.append('--root')
  if develop:
    cmd.append('--develop')

  # We need to set this option as otherwise the dependencies that we JUST
  # bootstrapped will be considered as already satsified, even though they
  # will not be after PPYM was installed in root or global level.
  cmd.append('--pip-separate-process')

  print("Installing PPYM ({}) ...".format(' '.join(cmd)))
  cmd.append(__directory__)
  require('./index').main(cmd, standalone_mode=False)

  local = (not global_ and not root)
  if not local and not existed_before and bootstrap and os.path.isdir('nodepy_modules'):
    print('Cleaning up bootstrap modules directory ...')
    shutil.rmtree('nodepy_modules')


main()
