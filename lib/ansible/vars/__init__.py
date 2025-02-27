# (c) 2012-2014, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

# Make coding more python3-ish
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

from collections import defaultdict
from collections import MutableMapping

try:
    from hashlib import sha1
except ImportError:
    from sha import sha as sha1

from ansible import constants as C
from ansible.cli import CLI
from ansible.errors import *
from ansible.parsing import DataLoader
from ansible.plugins.cache import FactCache
from ansible.template import Templar
from ansible.utils.debug import debug
from ansible.vars.hostvars import HostVars

CACHED_VARS = dict()

class VariableManager:

    def __init__(self):

        self._fact_cache       = FactCache()
        self._vars_cache       = defaultdict(dict)
        self._extra_vars       = defaultdict(dict)
        self._host_vars_files  = defaultdict(dict)
        self._group_vars_files = defaultdict(dict)
        self._inventory        = None

        self._omit_token       = '__omit_place_holder__%s' % sha1(os.urandom(64)).hexdigest()

    def _get_cache_entry(self, play=None, host=None, task=None):
        play_id = "NONE"
        if play:
            play_id = play._uuid

        host_id = "NONE"
        if host:
            host_id = host.get_name()

        task_id = "NONE"
        if task:
            task_id = task._uuid

        return "PLAY:%s;HOST:%s;TASK:%s" % (play_id, host_id, task_id)

    @property
    def extra_vars(self):
        ''' ensures a clean copy of the extra_vars are made '''
        return self._extra_vars.copy()

    @extra_vars.setter
    def extra_vars(self, value):
        ''' ensures a clean copy of the extra_vars are used to set the value '''
        assert isinstance(value, MutableMapping)
        self._extra_vars = value.copy()

    def set_inventory(self, inventory):
        self._inventory = inventory

    def _validate_both_dicts(self, a, b):
        '''
        Validates that both arguments are dictionaries, or an error is raised.
        '''
        if not (isinstance(a, MutableMapping) and isinstance(b, MutableMapping)):
            raise AnsibleError("failed to combine variables, expected dicts but got a '%s' and a '%s'" % (type(a).__name__, type(b).__name__))

    def _combine_vars(self, a, b):
        '''
        Combines dictionaries of variables, based on the hash behavior
        '''

        self._validate_both_dicts(a, b)

        if C.DEFAULT_HASH_BEHAVIOUR == "merge":
            return self._merge_dicts(a, b)
        else:
            return dict(a.items() + b.items())

    def _merge_dicts(self, a, b):
        '''
        Recursively merges dict b into a, so that keys
        from b take precedence over keys from a.
        '''

        result = dict()

        self._validate_both_dicts(a, b)

        for dicts in a, b:
            # next, iterate over b keys and values
            for k, v in dicts.iteritems():
                # if there's already such key in a
                # and that key contains dict
                if k in result and isinstance(result[k], dict):
                    # merge those dicts recursively
                    result[k] = self._merge_dicts(a[k], v)
                else:
                    # otherwise, just copy a value from b to a
                    result[k] = v

        return result

    def get_vars(self, loader, play=None, host=None, task=None, use_cache=True):
        '''
        Returns the variables, with optional "context" given via the parameters
        for the play, host, and task (which could possibly result in different
        sets of variables being returned due to the additional context).

        The order of precedence is:
        - play->roles->get_default_vars (if there is a play context)
        - group_vars_files[host] (if there is a host context)
        - host_vars_files[host] (if there is a host context)
        - host->get_vars (if there is a host context)
        - fact_cache[host] (if there is a host context)
        - vars_cache[host] (if there is a host context)
        - play vars (if there is a play context)
        - play vars_files (if there's no host context, ignore
          file names that cannot be templated)
        - task->get_vars (if there is a task context)
        - extra vars
        '''

        debug("in VariableManager get_vars()")
        cache_entry = self._get_cache_entry(play=play, host=host, task=task)
        if cache_entry in CACHED_VARS and use_cache:
            debug("vars are cached, returning them now")
            return CACHED_VARS[cache_entry]

        all_vars = defaultdict(dict)

        if play:
            # first we compile any vars specified in defaults/main.yml
            # for all roles within the specified play
            for role in play.get_roles():
                all_vars = self._combine_vars(all_vars, role.get_default_vars())

        if host:
            # next, if a host is specified, we load any vars from group_vars
            # files and then any vars from host_vars files which may apply to
            # this host or the groups it belongs to

            # we merge in the special 'all' group_vars first, if they exist
            if 'all' in self._group_vars_files:
                all_vars = self._combine_vars(all_vars, self._group_vars_files['all'])

            for group in host.get_groups():
                all_vars = self._combine_vars(all_vars, group.get_vars())
                if group.name in self._group_vars_files and group.name != 'all':
                    all_vars = self._combine_vars(all_vars, self._group_vars_files[group.name])

            host_name = host.get_name()
            if host_name in self._host_vars_files:
                all_vars = self._combine_vars(all_vars, self._host_vars_files[host_name])

            # then we merge in vars specified for this host
            all_vars = self._combine_vars(all_vars, host.get_vars())

            # next comes the facts cache and the vars cache, respectively
            try:
                all_vars = self._combine_vars(all_vars, self._fact_cache.get(host.name, dict()))
            except KeyError:
                pass

        if play:
            all_vars = self._combine_vars(all_vars, play.get_vars())
            templar = Templar(loader=loader, variables=all_vars)

            for vars_file_item in play.get_vars_files():
                try:
                    # we assume each item in the list is itself a list, as we
                    # support "conditional includes" for vars_files, which mimics
                    # the with_first_found mechanism.
                    vars_file_list = templar.template(vars_file_item)
                    if not isinstance(vars_file_list, list):
                         vars_file_list = [ vars_file_list ]

                    # now we iterate through the (potential) files, and break out
                    # as soon as we read one from the list. If none are found, we
                    # raise an error, which is silently ignored at this point.
                    for vars_file in vars_file_list:
                        data = loader.load_from_file(vars_file)
                        if data is not None:
                            all_vars = self._combine_vars(all_vars, data)
                            break
                    else:
                        raise AnsibleError("vars file %s was not found" % vars_file_item)
                except:
                    # FIXME: get_vars should probably be taking a flag to determine
                    #        whether or not vars files errors should be fatal at this
                    #        stage, or just base it on whether a host was specified?
                    pass

            if not C.DEFAULT_PRIVATE_ROLE_VARS:
                for role in play.get_roles():
                    all_vars = self._combine_vars(all_vars, role.get_vars())

        if host:
            all_vars = self._combine_vars(all_vars, self._vars_cache.get(host.get_name(), dict()))

        if task:
            if task._role:
                all_vars = self._combine_vars(all_vars, task._role.get_vars())
            all_vars = self._combine_vars(all_vars, task.get_vars())

        all_vars = self._combine_vars(all_vars, self._extra_vars)

        # FIXME: make sure all special vars are here
        # Finally, we create special vars

        all_vars['playbook_dir'] = loader.get_basedir()

        if host:
            all_vars['groups'] = [group.name for group in host.get_groups()]

            if self._inventory is not None:
                hostvars = HostVars(vars_manager=self, play=play, inventory=self._inventory, loader=loader)
                all_vars['hostvars'] = hostvars
                all_vars['groups']   = self._inventory.groups_list()

        if task:
            if task._role:
                all_vars['role_path'] = task._role._role_path

        if self._inventory is not None:
            all_vars['inventory_dir'] = self._inventory.basedir()
            if play:
                # add the list of hosts in the play, as adjusted for limit/filters
                # FIXME: play_hosts should be deprecated in favor of ansible_play_hosts,
                #        however this would take work in the templating engine, so for now
                #        we'll add both so we can give users something transitional to use
                host_list = [x.name for x in self._inventory.get_hosts()]
                all_vars['play_hosts'] = host_list
                all_vars['ansible_play_hosts'] = host_list


        # the 'omit' value alows params to be left out if the variable they are based on is undefined
        all_vars['omit'] = self._omit_token

        all_vars['ansible_version'] = CLI.version_info(gitinfo=False)

        # make vars self referential, so people can do things like 'vars[var_name]'
        copied_vars = all_vars.copy()
        if 'hostvars' in copied_vars:
            del copied_vars['hostvars']
        all_vars['vars'] = all_vars.copy()

        #CACHED_VARS[cache_entry] = all_vars

        debug("done with get_vars()")
        return all_vars

    def _get_inventory_basename(self, path):
        '''
        Returns the bsaename minus the extension of the given path, so the
        bare filename can be matched against host/group names later
        '''

        (name, ext) = os.path.splitext(os.path.basename(path))
        if ext not in ('.yml', '.yaml'):
            return os.path.basename(path)
        else:
            return name

    def _load_inventory_file(self, path, loader):
        '''
        helper function, which loads the file and gets the
        basename of the file without the extension
        '''

        if loader.is_directory(path):
            data = dict()

            try:
                names = loader.list_directory(path)
            except os.error as err:
                raise AnsibleError("This folder cannot be listed: %s: %s." % (path, err.strerror))

            # evaluate files in a stable order rather than whatever
            # order the filesystem lists them.
            names.sort()

            # do not parse hidden files or dirs, e.g. .svn/
            paths = [os.path.join(path, name) for name in names if not name.startswith('.')]
            for p in paths:
                _found, results = self._load_inventory_file(path=p, loader=loader)
                if results is not None:
                    data = self._combine_vars(data, results)

        else:
            file_name, ext = os.path.splitext(path)
            data = None
            if not ext:
                for ext in C.YAML_FILENAME_EXTENSIONS:
                    new_path = path + ext
                    if loader.path_exists(new_path):
                        data = loader.load_from_file(new_path)
                        break
            else:
                if loader.path_exists(path):
                    data = loader.load_from_file(path)

        name = self._get_inventory_basename(path)
        return (name, data)

    def add_host_vars_file(self, path, loader):
        '''
        Loads and caches a host_vars file in the _host_vars_files dict,
        where the key to that dictionary is the basename of the file, minus
        the extension, for matching against a given inventory host name
        '''

        (name, data) = self._load_inventory_file(path, loader)
        if data:
            self._host_vars_files[name] = data
            return data
        else:
            return dict()

    def add_group_vars_file(self, path, loader):
        '''
        Loads and caches a host_vars file in the _host_vars_files dict,
        where the key to that dictionary is the basename of the file, minus
        the extension, for matching against a given inventory host name
        '''

        (name, data) = self._load_inventory_file(path, loader)
        if data:
            self._group_vars_files[name] = data
            return data
        else:
            return dict()

    def set_host_facts(self, host, facts):
        '''
        Sets or updates the given facts for a host in the fact cache.
        '''

        assert isinstance(facts, dict)

        if host.name not in self._fact_cache:
            self._fact_cache[host.name] = facts
        else:
            try:
                self._fact_cache[host.name].update(facts)
            except KeyError:
                self._fact_cache[host.name] = facts

    def set_host_variable(self, host, varname, value):
        '''
        Sets a value in the vars_cache for a host.
        '''

        host_name = host.get_name()
        if host_name not in self._vars_cache:
            self._vars_cache[host_name] = dict()
        self._vars_cache[host_name][varname] = value

