# Copyright 2014 ARM Limited
#
# Licensed under the Apache License, Version 2.0
# See LICENSE file for details.

# standard library modules, , ,
import json
import os
from collections import OrderedDict
import tarfile
import re
import logging
import errno

# PyPi/standard library > 3.4
# it has to be PurePath
from pathlib import PurePath

# version, , represent versions and specifications, internal
import version
# Ordered JSON, , read & write json, internal
import ordered_json
# vcs, , represent version controlled directories, internal
import vcs
# fsutils, , misc filesystem utils, internal
import fsutils
# Registry Access, , access packages in the registry, internal
import registry_access

# These patterns are used in addition to any glob expressions defined by the
# .yotta_ignore file
Default_Publish_Ignore = [
    'upload.tar.[gb]z',
    '.git',
    '.hg',
    '.svn',
    'yotta_modules',
    'yotta_targets',
    'build',
    '.DS_Store',
    '.sw[ponml]',
    '._.*',
    '~',
    '.DS_Store',
    '._.*',
]

Readme_Regex = re.compile('^readme(?:\.md)', re.IGNORECASE)

Ignore_List_Fname = '.yotta_ignore'

logger = logging.getLogger('components')

# OptionalFileWrapper provides a scope object that can wrap a none-existent file
class OptionalFileWrapper(object):
    def __init__(self, fname=None, mode=None):
        self.fname = fname
        self.mode = mode
        super(OptionalFileWrapper, self).__init__()
    def __enter__(self):
        if self.fname:
            self.file = open(self.fname, self.mode)
        else:
            self.file = open(os.devnull)
        return self
    def __exit__(self, type, value, traceback):
        self.file.close()
    def contents(self):
        if self.fname:
            return self.file.read()
        else:
            return ''
    def extension(self):
        if self.fname:
            return os.path.splitext(self.fname)[1]
        else:
            return ''
    
    def __nonzero__(self):
        return bool(self.fname)

# Pack represents the common parts of Target and Component objects (versions,
# VCS, etc.)

class Pack(object):
    def __init__(self, path, description_filename, installed_linked, latest_suitable_version=None):
        self.path = path
        self.installed_linked = installed_linked
        self.vcs = None
        self.error = None
        self.latest_suitable_version = latest_suitable_version
        self.version = None
        self.description_filename = description_filename
        self.ignore_list_fname = Ignore_List_Fname
        self.ignore_patterns = [re.compile('|'.join(Default_Publish_Ignore))]
        try:
            self.description = ordered_json.load(os.path.join(path, description_filename))
            self.version = version.Version(self.description['version'])
        except Exception, e:
            self.description = OrderedDict()
            self.error = e
        try:
            with open(os.path.join(path, self.ignore_list_fname), 'r') as ignorefile:
                self.ignore_patterns += self._parseIgnoreFile(ignorefile)
        except IOError as e: 
            if e.errno != errno.ENOENT:
                raise
        self.vcs = vcs.getVCS(path)

    def exists(self):
        return os.path.exists(self.description_filename)
    
    def getError(self):
        ''' If this isn't a valid component/target, return some sort of
            explanation about why that is. '''
        return self.error

    def getDescriptionFile(self):
        return os.path.join(self.path, self.description_filename)

    def installedLinked(self):
        return self.installed_linked

    def setLatestAvailable(self, version):
        self.latest_suitable_version = version

    def outdated(self):
        ''' Return a truthy object if a newer suitable version is available,
            otherwise return None.
            (in fact the object returned is a ComponentVersion that can be used
             to get the newer version)
        '''
        if self.latest_suitable_version and self.latest_suitable_version > self.version:
            return self.latest_suitable_version
        else:
            return None

    def vcsIsClean(self):
        ''' Return true if the directory is not version controlled, or if it is
            version controlled with a supported system and is in a clean state
        '''
        if not self.vcs:
            return True
        return self.vcs.isClean()

    def commitVCS(self, tag=None):
        ''' Commit the current working directory state (or do nothing if the
            working directory is not version controlled)
        '''
        if not self.vcs:
            return
        self.vcs.commit(message='version %s' % tag, tag=tag)

    def getVersion(self):
        ''' Return the version as specified by the package file.
            This will always be a real version: 1.2.3, not a hash or a URL.

            Note that a component installed through a URL still provides a real
            version - so if the first component to depend on some component C
            depends on it via a URI, and a second component depends on a
            specific version 1.2.3, dependency resolution will only succeed if
            the version of C obtained from the URL happens to be 1.2.3
        '''
        return self.version

    def getName(self):
        if self.description:
            return self.description['name']
        else:
            return None
    
    def _parseIgnoreFile(self, f):
        r = []
        for l in f:
            l = l.rstrip('\n\r')
            if not l.startswith('#'):
                r.append(l)
        return r

    def ignores(self, path):
        ''' Test if this module ignores the file at "path" '''
        test_path = PurePath(path)

        for exp in self.ignore_patterns:
            if test_path.match(exp):
                logger.debug('"%s" ignored' % path)
                return True
        return False

    def setVersion(self, version):
        self.version = version
        self.description['version'] = str(self.version)

    def setName(self, name):
        self.description['name'] = name

    def writeDescription(self):
        ''' Write the current (possibly modified) component description to a
            package description file in the component directory.
        '''
        ordered_json.dump(os.path.join(self.path, self.description_filename), self.description)
        if self.vcs:
            self.vcs.markForCommit(self.description_filename)
    
    def generateTarball(self, file_object):
        ''' Write a tarball of the current component/target to the file object
            "file_object", which must already be open for writing at position 0
        '''
        archive_name = '%s-%s' % (self.getName(), self.getVersion())
        def filterArchive(tarinfo):
            if tarinfo.name.find(archive_name) == 0 :
                unprefixed_name = tarinfo.name[len(archive_name)+1:]
            else:
                unprefixed_name = tarinfo.name
            if self.ignores(unprefixed_name):
                return None
            else:
                return tarinfo
        with tarfile.open(fileobj=file_object, mode='w:gz') as tf:
            logger.info('generate archive extracting to "%s"' % archive_name)
            tf.add(self.path, arcname=archive_name, filter=filterArchive)
    
    def findAndOpenReadme(self):
        files = os.listdir(self.path)
        readme_files = filter(lambda x: Readme_Regex.match(x), files)
        reamde_file_if_found = None
        for f in readme_files:
            if f.endswith('.md'):
                return OptionalFileWrapper(f, 'r')
        if len(readme_files):
            # if we have multiple files and none of them end with .md, then we're
            # in some hellish world where files have the same name with different
            # casing. Just choose the first in the directory listing:
            return OptionalFileWrapper(readme_files[0], 'r')
        else:
            # no readme files: return an empty file wrapper
            return OptionalFileWrapper()

    def publish(self):
        ''' Publish to the appropriate registry, return a description of any
            errors that occured, or None if successful.
            No VCS tagging is performed.
        '''
        upload_archive = os.path.join(self.path, 'upload.tar.gz')
        fsutils.rmF(upload_archive)
        fd = os.open(upload_archive, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        with os.fdopen(fd, 'rb+') as tar_file:
            tar_file.truncate()
            self.generateTarball(tar_file)
            tar_file.seek(0)
            with self.findAndOpenReadme() as readme_file_wrapper:
                if not readme_file_wrapper:
                    logger.warning("no readme.md file detected")
                with open(self.getDescriptionFile(), 'r') as description_file:
                    return registry_access.publish(
                        self.getRegistryNamespace(),
                        self.getName(),
                        self.getVersion(),
                        description_file,
                        tar_file,
                        readme_file_wrapper.file,
                        readme_file_wrapper.extension().lower()
                    )

    @classmethod
    def ensureOrderedDict(cls, sequence=None):
        # !!! NB: MUST return the same object if the object is already an
        # ordered dictionary. we rely on spooky-action-at-a-distance to keep
        # the "available components" dictionary synchronised between all users
        if isinstance(sequence, OrderedDict):
            return sequence
        elif sequence:
            return OrderedDict(sequence)
        else:
            return OrderedDict()

    def __repr__(self):
        if not self:
            return "INVALID COMPONENT @ %s: %s" % (self.path, self.description)
        return "%s %s at %s" % (self.description['name'], self.description['version'], self.path)

    # provided for truthiness testing, we test true only if we successfully
    # read a package file
    def __nonzero__(self):
        return bool(self.description)
