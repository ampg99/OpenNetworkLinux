#!/usr/bin/python
############################################################
#
# ONL Root Filesystem Generator
#
############################################################
import argparse
import os
import sys
import logging
import yaml
import tempfile
import shutil
import pprint
import fcntl
import subprocess
import glob
import submodules
import StringIO
from collections import Iterable
import onlyaml
import onlu
import fileinput
import crypt
import string
import random
import re
import json

logger = onlu.init_logging('onlrfs')

class OnlRfsError(Exception):
    """General Error Exception"""
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)




class OnlRfsSystemAdmin(object):

    def __init__(self, chroot):
        self.chroot = chroot

    @staticmethod
    def gen_salt():
        # return an eight character salt value
        salt_map = './' + string.digits + string.ascii_uppercase + \
            string.ascii_lowercase
        rand = random.SystemRandom()
        salt = ''
        for i in range(0, 8):
            salt += salt_map[rand.randint(0, 63)]
        return salt

    @staticmethod
    def chmod(mode, file_):
        onlu.execute("sudo chmod %s %s" % (mode, file_),
                     ex=OnlRfsError("Could not change permissions (%s) on file %s" % (mode, file_)))

    @staticmethod
    def chown(file_, ownspec):
        onlu.execute("sudo chown %s %s" % (ownspec, file_),
                     ex=OnlRfsError("Could not change ownership (%s) on file %s" % (ownspec, file_)))

    def userdel(self, username):
        pf = os.path.join(self.chroot, 'etc/passwd')
        sf = os.path.join(self.chroot, 'etc/shadow')

        self.chmod("a+rwx", os.path.dirname(pf))
        self.chmod("a+rw", pf);
        self.chmod("a+rw", sf);

        # Can't use the userdel command because of potential uid 0 in-user problems while running ourselves
        for line in fileinput.input(pf, inplace=True):
            if not line.startswith('%s:' % username):
                print line,
        for line in fileinput.input(sf, inplace=True):
            if not line.startswith('%s:' % username):
                print line,

        self.chmod("go-wx", pf);
        self.chmod("go-wx", sf);

    def useradd(self, username, uid=None, gid=None, password=None, shell='/bin/bash', home=None, groups=None, sudo=False, deleteFirst=True):
        args = [ 'useradd', '--create-home' ]

        if uid:
            args = args + [ '--non-unique', '--uid', str(uid) ]

        if password:
            epassword=crypt.crypt(password, '$1$%s$' % self.gen_salt());
            args = args + ['-p', epassword ]

        if shell:
            args = args + [ '--shell', shell ]

        if gid:
            args = args + [ '--gid', gid ]

        if home:
            args = args + [ '--home', home ]

        if groups:
            args = args + [ '--group', groups ]

        if deleteFirst:
            self.userdel(username)

        args.append(username)

        onlu.execute(args,
                     chroot=self.chroot,
                     ex=OnlRfsError("Adding user '%s' failed." % username))

        if password is None:
            onlu.execute("passwd -d %s" % username,
                         chroot=self.chroot,
                         ex=OnlRfsError("Error deleting password for user '%s'" % username))

        logger.info("user %s password %s", username, password)

        if sudo:
            sudoer = os.path.join(self.chroot, 'etc/sudoers.d', username)
            self.chmod("777", os.path.dirname(sudoer))
            with open(sudoer, "w") as f:
                f.write("%s ALL=(ALL:ALL) NOPASSWD:ALL\n" % username);
            self.chmod("0440", sudoer)
            self.chown(sudoer, "root:root")
            self.chmod("755", os.path.dirname(sudoer))

    def user_password_set(self, username, password):
        logger.info("user %s password now %s", username, password)
        epassword=crypt.crypt(password, '$1$%s$' % self.gen_salt());
        onlu.execute(['usermod', '-p', epassword, username],
                     chroot=self.chroot,
                     ex=OnlRfsError("Error setting password for user '%s'" % username))

    def user_shell_set(self, username, shell):
        onlu.execute('chsh --shell %s %s' % (shell, username), chroot=self.chroot,
                     ex=OnlRfsError("Error setting shell %s for user %s" % (shell, username)))

    def user_disable(self, username):
        self.user_shell_set(username, '/bin/false')



class OnlMultistrapConfig(object):
    def __init__(self, config):
        self.config = config
        self.__validate()

    def __validate_key(self, d, k, type_, required=False):
        pass

    def __validate(self):
        """Validate the contents of our configuration."""

        # There must be a General section.
        general = self.config.get('General', None)

        if general is None:
            raise OnlRfsError("Section 'General' is missing from the multistrap configuration.")

        self.__validate_key(general, 'arch', str, True)
        self.__validate_key(general, 'debootstrap', str, True)

        for e in general['debootstrap'].split():
            if e not in self.config:
                raise OnlRfsError("Section '%s' is specified in the debootstrap option but does not exist in the configuration." % e)

        self.localrepos = []

    def generate_handle(self, handle):
        for (name, fields) in self.config.iteritems():
            handle.write("[%s]\n" % name)
            for (k,v) in fields.iteritems():

                if type(v) is bool:
                    v = 'true' if v == True else 'false'
                if type(v) is list:
                    v = " ".join(onlu.sflatten(v))

                if k == 'source' and os.path.exists(v):
                    self.localrepos.append(v)
                    v = "copy:%s ./" % v

                if k == 'packages' and type(v) is list:
                    raise OnlRfsError("packages=%s" % v)

                handle.write("%s=%s\n" % (k, v))
            handle.write("\n")

    def generate_file(self, fname=None):
        if fname is None:
            h = tempfile.NamedTemporaryFile(delete=False)
            fname = h.name
        elif fname == '-' or fname == 'stdout':
            h = sys.stdout
        else:
            h = open(fname, "w")
        self.generate_handle(h)
        return fname

    def get_packages(self):
        pkgs = []
        for (name, fields) in self.config.iteritems():
            for (k,v) in fields.iteritems():
                if k == 'packages':
                    if type(v) is list:
                        pkgs = pkgs + list(onlu.sflatten(v))
                    else:
                        pkgs = pkgs + v.split()
        return pkgs

    def __str__(self):
        handle = StringIO.StringIO()
        self.generate_handle(handle)
        return handle.getvalue()


class OnlRfsBuilder(object):

    DEFAULTS = dict(
        DEBIAN_SUITE='wheezy',
        DEBIAN_MIRROR='mirrors.kernel.org/debian/',
        APT_CACHE='127.0.0.1:3142/'
        )

    MULTISTRAP='/usr/sbin/multistrap'
    QEMU_PPC='/usr/bin/qemu-ppc-static'
    QEMU_ARM='/usr/bin/qemu-arm-static'
    BINFMT_PPC='/proc/sys/fs/binfmt_misc/qemu-ppc'

    def __init__(self, config, arch, **kwargs):
        self.kwargs = kwargs
        self.arch = arch
        self.kwargs['ARCH'] = arch
        self.kwargs.update(self.DEFAULTS)
        self.__load(config)
        self.__validate()

    def __load(self, config):
        if not os.path.exists(config):
            raise OnlRfsError("Configuration file '%s' does not exist." % config)
        self.config = onlyaml.loadf(config, self.kwargs)

    def __validate(self):

        if not os.path.exists(self.MULTISTRAP):
            raise OnlRfsError("%s is missing." % self.MULTISTRAP)

        if self.arch == 'powerpc':
            if not os.path.exists(self.QEMU_PPC):
                raise OnlRfsError("%s is missing." % self.QEMU_PPC)

        if self.arch == 'armel':
            if not os.path.exists(self.QEMU_ARM):
                raise OnlRfsError("%s is missing." % self.QEMU_ARM)

        if not 'Multistrap' in self.config:
            raise OnlRfsError("The Multistrap configuration section is missing.")

        self.ms = OnlMultistrapConfig(self.config['Multistrap'])


    def get_packages(self):
        return self.ms.get_packages()

    def msconfig(self, fname):
        return self.ms.generate_file(fname)

    def multistrap(self, dir_):
        msconfig = self.ms.generate_file()

        # Optional local package updates
        for r in self.ms.localrepos:
            logger.info("Updating %s" % r)
            if os.path.exists(os.path.join(r, 'Makefile')):
                onlu.execute("make -C %s" % r)

        if os.path.exists(dir_):
            onlu.execute("sudo rm -rf %s" % dir_,
                         ex=OnlRfsError("Could not remove target directory."))

        if onlu.execute("sudo %s -d %s -f %s" % (self.MULTISTRAP, dir_, msconfig)) == 100:
            raise OnlRfsError("Multistrap APT failure.")

        if os.getenv("MULTISTRAP_DEBUG"):
            raise OnlRfsError("Multistrap debug.")


    def dpkg_configure(self, dir_):
        if self.arch == 'powerpc':
            onlu.execute('sudo cp %s %s' % (self.QEMU_PPC, os.path.join(dir_, 'usr/bin')))
        if self.arch == 'armel':
            onlu.execute('sudo cp %s %s' % (self.QEMU_ARM, os.path.join(dir_, 'usr/bin')))

        script = os.path.join(dir_, "tmp/configure.sh")
        with open(script, "w") as f:
            os.chmod(script, 0700)
            f.write("""
#!/bin/bash -ex
/bin/echo -e "#!/bin/sh\\nexit 101" >/usr/sbin/policy-rc.d
chmod +x /usr/sbin/policy-rc.d
export DEBIAN_FRONTEND=noninteractive
export DEBCONF_NONINTERACTIVE_SEEN=true
echo "127.0.0.1 localhost" >/etc/hosts
touch /etc/fstab
echo "localhost" >/etc/hostname
if [ -f /var/lib/dpkg/info/dash.preinst ]; then
    /var/lib/dpkg/info/dash.preinst install
fi
if [ -f /usr/sbin/locale-gen ]; then
    echo "en_US.UTF-8 UTF-8" >/etc/locale.gen
    /usr/sbin/locale-gen
    update-locale LANG=en_US.UTF-8
fi

dpkg --configure -a || true
dpkg --configure -a # configure any packages that failed the first time and abort on failure.

rm -f /usr/sbin/policy-rc.d
    """)

        logger.info("dpkg-configure filesystem...")

        onlu.execute("sudo chroot %s /tmp/configure.sh" % dir_,
                     ex=OnlRfsError("Post Configuration failed."))
        os.unlink(script)



    def configure(self, dir_):

        try:

            onlu.execute("sudo mount -t devtmpfs dev %s" % os.path.join(dir_, "dev"),
                         ex=OnlRfsError("Could not mount dev in new filesystem."))

            onlu.execute("sudo mount -t proc proc %s" % os.path.join(dir_, "proc"),
                         ex=OnlRfsError("Could not mount proc in new filesystem."))

            script = os.path.join(dir_, "tmp/configure.sh")

            if not os.getenv('NO_DPKG_CONFIGURE'):
                self.dpkg_configure(dir_)

            Configure = self.config.get('Configure', None)
            if Configure:
                for overlay in Configure.get('overlays', []):
                    logger.info("Overlay %s..." % overlay)
                    onlu.execute('tar -C %s -c --exclude "*~" . | sudo tar -C %s -x -v --no-same-owner' % (overlay, dir_),
                                 ex=OnlRfsError("Overlay '%s' failed." % overlay))

                for update in Configure.get('update-rc.d', []):
                    onlu.execute("sudo chroot %s /usr/sbin/update-rc.d %s" % (dir_, update),
                                 ex=OnlRfsError("update-rc.d %s failed." % (update)))

                for script in Configure.get('scripts', []):
                    logger.info("Configuration script %s..." % script)
                    onlu.execute("sudo %s %s" % (script, dir_),
                                 ex=OnlRfsError("script '%s' failed." % script))


                for command in Configure.get('commands', []):
                    if '__rfs__' in command:
                        command = command % dict(__rfs__=dir_)
                    logger.info("Configuration command '%s'..." % command)
                    onlu.execute(command,
                                 ex=OnlRfsError("Command '%s' failed." % command))

                for (user, values) in Configure.get('users', {}).iteritems():
                    ua = OnlRfsSystemAdmin(dir_)

                    if user == 'root':
                        if 'password' in values:
                            ua.user_password_set(user, values['password'])
                    else:
                        ua.useradd(username=user, **values)


                options = Configure.get('options', {})
                if options.get('clean', False):
                    logger.info("Cleaning Filesystem...")
                    onlu.execute('sudo chroot %s /usr/bin/apt-get clean' % dir_)
                    onlu.execute('sudo chroot %s /usr/sbin/localepurge' % dir_ )
                    onlu.execute('sudo chroot %s find /usr/share/doc -type f -delete' % dir_)
                    onlu.execute('sudo chroot %s find /usr/share/man -type f -delete' % dir_)


                if not options.get('securetty', True):
                    f = os.path.join(dir_, 'etc/securetty')
                    if os.path.exists(f):
                        logger.info("Removing %s" % f)
                        onlu.execute('sudo rm %s' % f,
                                     ex=OnlRfsError('Could not remove file %s' % f))

                if not options.get('ttys', False):
                    f = os.path.join(dir_, 'etc/inittab')
                    ua.chmod('a+w', f)
                    ua.chmod('a+w', os.path.dirname(f))

                    logger.info("Clearing %s ttys..." % f)
                    for line in fileinput.input(f, inplace=True):
                        if re.match("^[123456]:.*", line):
                           line = "#" + line
                        print line,

                    ua.chmod('go-w', f)
                    ua.chmod('go-w', os.path.dirname(f))

                if options.get('console', True):
                    logger.info('Configuring Console Access in %s' % f)
                    f = os.path.join(dir_, 'etc/inittab')
                    ua.chmod('a+w', f)
                    ua.chmod('a+w', os.path.dirname(f))
                    with open(f, 'a') as h:
                        h.write("T0:23:respawn:/sbin/pgetty\n")
                    ua.chmod('go-w', f)
                    ua.chmod('go-w', os.path.dirname(f))


                for (mf, fields) in Configure.get('manifests', {}).iteritems():
                    logger.info("Configuring manifest %s..." % mf)
                    if mf.startswith('/'):
                        mf = mf[1:]
                    mname = os.path.join(dir_, mf)
                    onlu.execute("sudo mkdir -p %s" % os.path.dirname(mname))
                    onlu.execute("sudo touch %s" % mname)
                    onlu.execute("sudo chmod a+w %s" % mname)
                    md = {}
                    md['version'] = json.load(open(fields['version']))
                    md['arch'] = self.arch
                    if os.path.exists(fields['platforms']):
                        md['platforms'] = yaml.load(open(fields['platforms']))
                    else:
                        md['platforms'] = fields['platforms'].split(',')
                    with open(mname, "w") as f:
                        json.dump(md, f, indent=2)
                    onlu.execute("sudo chmod a-w %s" % mname)

                for (fname, v) in Configure.get('files', {}).get('add', {}).iteritems():
                    if fname.startswith('/'):
                        fname = fname[1:]
                    dst = os.path.join(dir_, fname)
                    onlu.execute("sudo mkdir -p %s" % os.path.dirname(dst))
                    onlu.execute("sudo touch %s" % dst)
                    onlu.execute("sudo chmod a+w %s" % dst)
                    if os.path.exists(v):
                        shutil.copy(v, dst)
                    else:
                        with open(dst, "w") as f:
                            f.write("%s\n" % v)

                for fname in Configure.get('files', {}).get('remove', []):
                    if fname.startswith('/'):
                        fname = fname[1:]
                    f = os.path.join(dir_, fname)
                    if os.path.exists(f):
                        onlu.execute("sudo rm -rf %s" % f)

                if Configure.get('issue'):
                    issue = Configure.get('issue')
                    fn = os.path.join(dir_, "etc/issue")
                    onlu.execute("sudo chmod a+w %s" % fn)
                    with open(fn, "w") as f:
                        f.write("%s\n\n" % issue)
                    onlu.execute("sudo chmod a-w %s" % fn)

                    fn = os.path.join(dir_, "etc/issue.net")
                    onlu.execute("sudo chmod a+w %s" % fn)
                    with open(fn, "w") as f:
                        f.write("%s" % issue)
                    onlu.execute("sudo chmod a-w %s" % fn)

        finally:
            onlu.execute("sudo umount -l %s %s" % (os.path.join(dir_, "dev"), os.path.join(dir_, "proc")))


if __name__ == '__main__':

    ap = argparse.ArgumentParser(description="ONL Root Filesystem Generator")
    ap.add_argument("--arch", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--dir")
    ap.add_argument("--show-packages", action='store_true')
    ap.add_argument("--no-build-packages", action='store_true')
    ap.add_argument("--msconfig")
    ap.add_argument("--multistrap-only", action='store_true')
    ap.add_argument("--no-multistrap", action='store_true')
    ap.add_argument("--cpio")
    ap.add_argument("--squash")

    ops = ap.parse_args()

    try:
        x = OnlRfsBuilder(ops.config, ops.arch)

        if ops.msconfig:
            x.msconfig(ops.msconfig)
            sys.exit(0)

        if ops.show_packages:
            print "\n".join(x.get_packages())
            sys.exit(0)

        if ops.dir is None:
            raise OnlRfsError("argument --dir is required")

        if not ops.no_build_packages:
            pkgs = x.get_packages()
            # Invoke onlpm to build all required (local) packages.
            onlu.execute("onlpm --try-arches %s all --skip-missing --require %s" % (ops.arch, " ".join(pkgs)),
                         ex=OnlRfsError("Failed to build all required packages."))


        if ops.multistrap_only:
            x.multistrap(ops.dir)
            sys.exit(0)

        if not ops.no_multistrap and not os.getenv('NO_MULTISTRAP'):
            x.multistrap(ops.dir)

        x.configure(ops.dir)

        if ops.cpio:
            if onlu.execute("make-cpio.sh %s %s" % (ops.dir, ops.cpio)) != 0:
                raise OnlRfsError("cpio creation failed.")

        if ops.squash:
            if os.path.exists(ops.squash):
                os.unlink(ops.squash)
            if onlu.execute("sudo mksquashfs %s %s -no-progress -noappend -comp gzip" % (ops.dir, ops.squash)) != 0:
                if os.path.exists(ops.squash):
                    os.unlink(ops.squash)
                raise OnlRfsError("Squash creation failed.")

    except (OnlRfsError, onlyaml.OnlYamlError), e:
        logger.error(e.value)
