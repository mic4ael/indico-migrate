# This file is part of Indico.
# Copyright (C) 2002 - 2017 European Organization for Nuclear Research (CERN).
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# Indico is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Indico; if not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function, unicode_literals

import sys
import time
import warnings
from collections import defaultdict
from operator import itemgetter

import click
from flask.exthook import ExtDeprecationWarning
from sqlalchemy.sql import func, select

warnings.simplefilter('ignore', ExtDeprecationWarning)  # some of our dependencies still use flask.ext :(

def _inject_unicode_debug(s, level=1):
    return s

# inject_unicode_debug happens to access the Config object
import indico.util.string as indico_util_string
indico_util_string.__dict__['inject_unicode_debug'] = _inject_unicode_debug

from indico.core.db.sqlalchemy import db
from indico.modules.groups import GroupProxy
from indico.modules.users.models.users import User
from indico.util.console import cformat, clear_line
from indico_migrate.migrate import migrate
from indico_migrate.namespaces import SharedNamespace
from indico_migrate.util import convert_to_unicode, MigrationStateManager, UnbreakingDB, get_storage

click.disable_unicode_literals_warning = True


def except_hook(exc_class, exception, tb):
    from IPython.core import ultratb
    ftb = ultratb.FormattedTB(mode='Verbose', color_scheme='Linux', call_pdb=1, include_vars=False)
    return ftb(exc_class, exception, tb)


@click.command()
@click.argument('sqlalchemy-uri')
@click.argument('zodb-uri')
@click.option('--verbose', '-v', is_flag=True, default=False, help="Use verbose output")
@click.option('--dblog', '-L', is_flag=True, default=False, help="Enable db query logging")
@click.option('--ldap-provider-name', default='legacy-ldap', help="Provider name to use for existing LDAP identities")
@click.option('--default-group-provider', required=True, help="Name of the default group provider")
@click.option('--ignore-local-accounts', is_flag=True, default=False, help="Do not migrate existing local accounts")
@click.option('--janitor-user-id', type=int, required=True, help="The ID of the Janitor user")
@click.option('--default-email', required=True, help="Fallback email in case of garbage")
@click.option('--archive-dir', required=True, multiple=True,
              help="The base path where resources are stored (ArchiveDir in indico.conf). When used multiple times, "
                   "the dirs are checked in order until a file is found.")
@click.option('--storage-backend', required=True,
              help="The name of the storage backend used for attachments.")
@click.option('--avoid-storage-check', is_flag=True,
              help="Avoid checking files in storage unless absolutely necessary due to encoding issues. This will "
                   "migrate all files with size=0.  When this option is specified, --archive-dir must be used exactly "
                   "once.")
@click.option('--symlink-backend', help="The name of the storage backend used for symlinks.")
@click.option('--symlink-target', help="If set, any files with a non-UTF8 path will be symlinked in this location and "
                                       "store the path to the symlink instead (relative to the archive dir). "
                                       "When this option is specified, --archive-dir must be used exactly once.")
@click.option('--rb-zodb-uri', required=False, help="ZODB URI for the room booking database")
@click.option('--photo-path', type=click.Path(exists=True, file_okay=False),
              help="path to the folder containing room photos")
@click.option('--debug', is_flag=True, default=False, help="Run migration in debug mode (requires ipython)")
@click.option('--save-restore', is_flag=True, default=False, help="Save a restore point in case of failure")
@click.option('--restore-file', type=click.File('r'), help="Restore migration from a file (enables debug)")
def cli(sqlalchemy_uri, zodb_uri, rb_zodb_uri, verbose, dblog, debug, restore_file, **kwargs):
    """
    This script migrates your database from ZODB/Indico 1.2 to PostgreSQL (2.0).

    You always need to specify both the SQLAlchemy connection URI and
    ZODB URI (both zeo:// and file:// work).
    """

    if restore_file:
        debug = True

    if debug:
        verbose = True
        sys.excepthook = except_hook

    zodb_root = UnbreakingDB(get_storage(zodb_uri)).open().root()

    Importer._global_ns = SharedNamespace('global_ns', zodb_root, {
        'user_favorite_categories': 'set',
        'room_mapping': 'dict',
        'venue_mapping': 'dict',
        'legacy_event_ids': 'dict',
        'legacy_category_ids': 'dict',
        'wf_registry': 'dict',
        'used_short_urls': 'dict',
        'legacy_survey_mapping': 'dict',
        'ip_domains': 'dict',
        'avatar_merged_user': 'dict',
        'all_groups': 'dict',
        'users_by_primary_email': 'dict',
        'users_by_secondary_email': 'dict',
        'users_by_email': 'dict'
    })

    migrate(zodb_root, rb_zodb_uri, sqlalchemy_uri, verbose=verbose, dblog=dblog, restore_file=restore_file,
            debug=debug, **kwargs)


class Importer(object):
    #: Specify plugins that need to be loaded for the import (e.g. to access its .settings property)
    plugins = frozenset()
    prefix = ''

    def __init__(self, app, sqlalchemy_uri, zodb_root, verbose, dblog, default_group_provider, tz, **kwargs):
        self.sqlalchemy_uri = sqlalchemy_uri
        self.quiet = not verbose
        self.dblog = dblog
        self.zodb_root = zodb_root
        self.app = app
        self.tz = tz
        self.default_group_provider = default_group_provider

        self.initialize_global_ns(Importer._global_ns)

    def initialize_global_ns(self, g):
        pass

    @property
    def makac_info(self):
        return self.zodb_root['MaKaCInfo']['main']

    @property
    def global_ns(self):
        return Importer._global_ns

    def __repr__(self):
        return '<{}({}, {})>'.format(type(self).__name__, self.sqlalchemy_uri, self.zodb_uri)

    def flushing_iterator(self, iterable, n=5000):
        """Iterates over `iterable` and flushes the ZODB cache every `n` items.

        :param iterable: an iterable object
        :param n: number of items to flush after
        """
        conn = self.zodb_root._p_jar
        for i, item in enumerate(iterable, 1):
            yield item
            if i % n == 0:
                conn.sync()

    def check_plugin_schema(self, name):
        """Checks if a plugin schema exists in the database.

        :param name: Name of the plugin
        """
        sql = 'SELECT COUNT(*) FROM "information_schema"."schemata" WHERE "schema_name" = :name'
        count = db.engine.execute(db.text(sql), name='plugin_{}'.format(name)).scalar()
        if not count:
            print(cformat('%{red!}Plugin schema does not exist%{reset}'))
            print(cformat('Run %{yellow!}indico db --plugin {} upgrade%{reset} to create it').format(name))
            return False
        return True

    def print_msg(self, msg, always=False):
        """Prints a message to the console.

        By default, messages are not shown in quiet mode, but this
        can be changed using the `always` parameter.
        """
        if self.quiet:
            if not always:
                return
            clear_line()
        print((self.prefix + msg).encode('utf-8'))

    def print_step(self, msg):
        """Prints a message about a migration step to the console

        This message is always shown, even in quiet mode.
        """
        self.print_msg(cformat('%{cyan,blue} > %{cyan!,blue}{:<30}').format(msg), True)

    def print_msg_type(self, msg_type, msg_type_color, msg, always=False, event_id=None):
        """Prints a colored message to the console."""
        parts = [
            cformat('%%{%s}{}%%{reset}' % msg_type_color).format(msg_type),
            cformat('%{white!}{:>6s}%{reset}').format(unicode(event_id)) if event_id is not None else None,
            msg
        ]
        self.print_msg(' '.join(filter(None, parts)), always)

    def print_info(self, msg, always=False, has_event=True):
        """Prints an info message to the console.

        By default, info messages are not shown in quiet mode.
        They are prefixed with blank spaces to align with other
        messages.

        When calling this in a loop that is invoked a lot, it is
        recommended to add an explicit ``if not self.quiet`` check
        to avoid expensive `cformat` or `format` calls for a message
        that is never displayed.
        """
        self.print_msg(' ' * (11 if has_event else 4) + msg, always)

    def print_success(self, msg, always=False, event_id=None):
        """Prints a success message to the console.

        By default, success messages are not shown in quiet mode.
        They are prefixed with three green plus signs.

        When calling this in a loop that is invoked a lot, it is
        recommended to add an explicit ``if not self.quiet`` check
        to avoid expensive `cformat` or `format` calls for a message
        that is never displayed.
        """
        self.print_msg_type('+++', 'green!', msg, always, event_id)

    def print_warning(self, msg, always=True, event_id=None):
        """Prints a warning message to the console.

        By default, warnings are displayed even in quiet mode.
        Warning messages are with three yellow exclamation marks.
        """
        self.print_msg_type('!!!', 'yellow!', msg, always, event_id)

    def print_error(self, msg, event_id=None):
        """Prints an error message to the console

        Errors are always displayed, even in quiet mode.
        They are prefixed with three red exclamation marks.
        """
        self.print_msg_type('!!!', 'red!', msg, True, event_id)

    def convert_principal(self, old_principal):
        """Converts a legacy principal to PrincipalMixin style"""
        if old_principal.__class__.__name__ == 'Avatar':
            principal = self.global_ns.avatar_merged_user.get(old_principal.id)
            if not principal and 'email' in old_principal.__dict__:
                email = convert_to_unicode(old_principal.__dict__['email']).lower()
                principal = self.global_ns.users_by_primary_email.get(
                    email, self.global_ns.users_by_secondary_email.get(email))
                if principal is not None:
                    self.print_warning('Using {} for {} (matched via {})'.format(principal, old_principal, email))
            if not principal:
                self.print_error("User {} doesn't exist".format(old_principal.id))
            return principal
        elif old_principal.__class__.__name__ == 'Group':
            assert int(old_principal.id) in self.global_ns.all_groups
            return GroupProxy(int(old_principal.id))
        elif old_principal.__class__.__name__ in {'CERNGroup', 'LDAPGroup', 'NiceGroup'}:
            return GroupProxy(old_principal.id, self.default_group_provider)

    def convert_principal_list(self, opt):
        """Convert ACL principals to new objects"""
        return set(filter(None, (self.convert_principal(principal) for principal in opt._PluginOption__value)))

    def fix_sequences(self, schema=None, tables=None):
        for name, cls in sorted(db.Model._decl_class_registry.iteritems(), key=itemgetter(0)):
            table = getattr(cls, '__table__', None)
            if table is None:
                continue
            elif schema is not None and table.schema != schema:
                continue
            elif tables is not None and cls.__tablename__ not in tables:
                continue
            # Check if we have a single autoincrementing primary key
            candidates = [col for col in table.c if col.autoincrement and col.primary_key]
            if len(candidates) != 1 or not isinstance(candidates[0].type, db.Integer):
                continue
            serial_col = candidates[0]
            sequence_name = '{}.{}_{}_seq'.format(table.schema, cls.__tablename__, serial_col.name)

            query = select([func.setval(sequence_name, func.max(serial_col) + 1)], table)
            db.session.execute(query)
        db.session.commit()


class TopLevelMigrationStep(Importer):
    def run(self):
        start = time.time()
        self.migrate()
        self.print_msg(cformat('%{cyan}{:.06f} seconds%{reset}\a').format((time.time() - start)))

    def migrate(self):
        raise NotImplementedError


def main():
    return cli()
