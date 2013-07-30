#!/usr/bin/python
#
# rhelup.py - commandline frontend for rhelup, the RHEL Upgrader.
#
# Copyright (C) 2012 Red Hat Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Author: Will Woods <wwoods@redhat.com>

import os, sys, time

from rhelup.util import call
from rhelup.download import RHELupDownloader, YumBaseError
from rhelup.sysprep import prep_upgrade, prep_boot, setup_media_mount
from rhelup.upgrade import RHELupUpgrade, TransactionError

from rhelup.commandline import parse_args, do_cleanup, device_setup
from rhelup import textoutput as output

import logging, rhelup.logutils, rhelup.media
log = logging.getLogger("rhelup")
def message(m):
    print m
    log.info(m)

from rhelup import _, kernelpath, initrdpath

def setup_downloader(version, instrepo=None, cacheonly=False, repos=[]):
    log.debug("setup_downloader(version=%s, repos=%s)", version, repos)
    f = RHELupDownloader(version=version, cacheonly=cacheonly)
    f.instrepoid = instrepo
    repo_cb = output.RepoCallback()
    repo_prog = output.RepoProgress(fo=sys.stderr)
    disabled_repos = f.setup_repos(callback=repo_cb,
                                   progressbar=repo_prog,
                                   repos=repos)
    disabled_repos = filter(lambda id: id != f.instrepoid, disabled_repos)
    if disabled_repos:
        print _("No upgrade available for the following repos") + ": " + \
                " ".join(disabled_repos)
        log.info("disabled repos: " + " ".join(disabled_repos))
    return f

def download_packages(f):
    updates = f.build_update_transaction(callback=output.DepsolveCallback(f))
    # check for empty upgrade transaction
    if not updates:
        print _('Your system is already upgraded!')
        print _('Finished. Nothing to do.')
        raise SystemExit(0)
    # clean out any unneeded packages from the cache
    f.clean_cache(keepfiles=(p.localPkg() for p in updates))
    # download packages
    f.download_packages(updates, callback=output.DownloadCallback())

    return updates

def transaction_test(pkgs):
    print _("testing upgrade transaction")
    pkgfiles = set(po.localPkg() for po in pkgs)
    fu = RHELupUpgrade()
    fu.setup_transaction(pkgfiles=pkgfiles)
    fu.test_transaction(callback=output.TransactionCallback(numpkgs=len(pkgfiles)))

def reboot():
    call(['systemctl', 'reboot'])

def main(args):
    if args.clean:
        do_cleanup(args)
        return

    if args.device or args.iso:
        device_setup(args)

    # Get our packages set up where we can use 'em
    print _("setting up repos...")
    f = setup_downloader(version=args.network,
                         cacheonly=args.cacheonly,
                         instrepo=args.instrepo,
                         repos=args.repos)

    if args.expire_cache:
        print "expiring cache files"
        f.cleanExpireCache()
        return
    if args.clean_metadata:
        print "cleaning metadata"
        f.cleanMetadata()
        return

    if args.skipkernel:
        message("skipping kernel/initrd download")
    elif f.instrepoid is None or f.instrepoid in f.disabled_repos:
        print _("Error: can't get boot images.")
        if args.instrepo:
            print _("The '%s' repo was rejected by yum as invalid.") % args.instrepo
            if args.iso:
                print _("The given ISO probably isn't an install DVD image.")
            elif args.device:
                print _("The media doesn't contain a valid install DVD image.")
        else:
            print _("The installation repo isn't available.")
            print "You need to specify one with --instrepo." # XXX temporary
        raise SystemExit(1)
    else:
        print _("getting boot images...")
        kernel, initrd = f.download_boot_images() # TODO: force arch?

    if args.skippkgs:
        message("skipping package download")
    else:
        print _("setting up update...")
        if len(f.pkgSack) == 0:
            print("no updates available in configured repos!")
            raise SystemExit(1)
        pkgs = download_packages(f)
        # Run a test transaction
        transaction_test(pkgs)

    # And prepare for upgrade
    # TODO: use polkit to get root privs for these things
    print _("setting up system for upgrade")
    if not args.skippkgs:
        prep_upgrade(pkgs)

    if not args.skipbootloader:
        if args.skipkernel:
            print "warning: --skipkernel without --skipbootloader"
            print "using default paths: %s %s" % (kernelpath, initrdpath)
            kernel = kernelpath
            initrd = initrdpath
        prep_boot(kernel, initrd)

    if args.device:
        setup_media_mount(args.device)

    if args.iso:
        rhelup.media.umount(args.device.mnt)

    if args.reboot:
        reboot()
    else:
        print _('Finished. Reboot to start upgrade.')

    #if f.disabled_repos:
        # NOTE: I hate having a hardcoded list of Important Repos here.
        # This information should be provided by the system, somehow..
        #important = ("fedora", "updates")
        #if any(i in f.disabled_repos for i in important):
        #    msg = _("WARNING: Some important repos could not be contacted: %s")
        #else:
        #    msg = _("NOTE: Some repos could not be contacted: %s")
        #print msg % ", ".join(f.disabled_repos)
        #print _("If you start the upgrade now, packages from these repos will not be installed.")

if __name__ == '__main__':
    args = parse_args()

    # TODO: use polkit to get privs for modifying bootloader stuff instead
    if os.getuid() != 0:
        print _("you must be root to run this program.")
        raise SystemExit(1)

    # set up logging
    if args.debuglog:
        rhelup.logutils.debuglog(args.debuglog)
    rhelup.logutils.consolelog(level=args.loglevel)
    log.info("%s starting at %s", sys.argv[0], time.asctime())

    try:
        main(args)
    except KeyboardInterrupt as e:
        print
        log.info("exiting on keyboard interrupt")
        if e.message:
            message(_("Exiting on keyboard interrupt (%s)") % e.message)
        raise SystemExit(1)
    except YumBaseError as e:
        print
        if isinstance(e.value, list):
            err = e.value.pop(0)
            message(_("Downloading failed: %s") % err)
            for p in e.value:
                message("  %s" % p)
        else:
            message(_("Downloading failed: %s") % e)
        log.debug("Exception:", exc_info=True)
        raise SystemExit(2)
    except TransactionError as e:
        print
        message(_("Upgrade test failed with the following problems:"))
        for s in e.summaries:
            message(s)
        log.debug("Detailed transaction problems:")
        for p in e.problems:
            log.debug(p)
        log.error(_("Upgrade test failed."))
        raise SystemExit(3)
    except Exception as e:
        log.info("Exception:", exc_info=True)
        raise
    finally:
        log.info("%s exiting at %s", sys.argv[0], time.asctime())
