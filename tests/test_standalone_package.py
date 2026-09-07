"""Installed-package, portable-path and public command contracts."""
import contextlib
import importlib
import io
import json
from pathlib import Path
import pkgutil
import tempfile
import unittest
from unittest.mock import patch
import pm2_animation_lab
from pm2_animation_lab import cli
from pm2_animation_lab.paths import code_root,is_data_directory
from pm2_animation_lab.pm2_activity_delivery_check import confined,DeliveryError
from pm2_animation_lab import pm2_activity_pipeline as pipeline


class InstalledPackageTests(unittest.TestCase):
    def test_every_module_imports_without_flat_sibling_modules(self):
        for item in pkgutil.iter_modules(pm2_animation_lab.__path__):
            if item.name=='__main__':continue
            with self.subTest(module=item.name):importlib.import_module('pm2_animation_lab.'+item.name)

    def test_bundled_profiles_survive_package_installation(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):self.assertEqual(cli.main(['doctor']),0)
        report=json.loads(out.getvalue())
        self.assertEqual(report['scene_profiles'],25)
        self.assertFalse(report['original_game_files_bundled'])

    def test_installed_cli_delegates_to_existing_publication_entry(self):
        args=['--request','r.json','--output','out','--quarantine-root','data','--source-root','sources']
        with patch.object(pipeline,'main',return_value=0) as delegated:
            self.assertEqual(cli.main(['pipeline']+args),0)
        delegated.assert_called_once_with(args)

    def test_installed_cli_keeps_playback_as_explicit_operation(self):
        args=['--request','r.json','--playback-directory','out','--quarantine-root','data','--source-root','sources']
        with patch.object(pipeline,'main',return_value=0) as delegated:
            self.assertEqual(cli.main(['pipeline']+args),0)
        delegated.assert_called_once_with(args)

    def test_binding_is_for_the_requested_file_and_missing_file_is_concise(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'input.json';p.write_text('{}')
            out=io.StringIO()
            with contextlib.redirect_stdout(out):self.assertEqual(cli.main(['bind',str(p)]),0)
            self.assertEqual(json.loads(out.getvalue()),pipeline.bind(p))
            err=io.StringIO()
            with contextlib.redirect_stderr(err):self.assertEqual(cli.main(['bind',str(p.parent/'missing')]),2)
            self.assertNotIn('Traceback',err.getvalue())


class PortableIsolationTests(unittest.TestCase):
    def test_installed_command_rejects_data_inside_another_tool_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'pyproject.toml').write_text('[project]\nname="pm2-animation-lab"\n')
            (root/'src/pm2_animation_lab').mkdir(parents=True);data=root/'private-data';data.mkdir()
            self.assertFalse(is_data_directory(data))

    def test_external_directory_does_not_require_a_drive_letter_or_fixed_name(self):
        with tempfile.TemporaryDirectory(prefix='different-name-') as directory:
            self.assertTrue(is_data_directory(directory))

    def test_tool_checkout_home_and_filesystem_root_cannot_be_data_roots(self):
        self.assertFalse(is_data_directory(code_root()))
        self.assertFalse(is_data_directory(Path.home()))
        self.assertFalse(is_data_directory(Path.cwd().anchor))

    def test_dotdot_escape_is_rejected_on_current_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'data';root.mkdir()
            with self.assertRaisesRegex(DeliveryError,'outside_quarantine'):confined(root/'..'/'outside',root)

    def test_symlink_escape_uses_resolved_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'data';root.mkdir();outside=Path(directory)/'outside';outside.mkdir()
            link=root/'alias'
            try:link.symlink_to(outside,target_is_directory=True)
            except OSError:self.skipTest('Host does not permit creating symlinks')
            with self.assertRaisesRegex(DeliveryError,'outside_quarantine'):confined(link/'file',root)


if __name__=='__main__':unittest.main()
