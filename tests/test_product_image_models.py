"""Tạo ảnh sản phẩm — tải file model (ONNX + thư viện tư thế) từ thư mục Drive về máy chủ (services/product_image_models.py).

Khoá: không đặt thư mục Drive thì không làm gì; đặt rồi thì thiếu biến nào báo đúng tên; chỉ tải file thiếu / khác cỡ,
giữ cấu trúc thư mục con; mỗi tiến trình chỉ tải một lần; chỉ tải nền khi chạy gunicorn. Drive giả lập, không gọi mạng.

Chạy: python manage.py test tests.test_product_image_models
"""
import os
import shutil
import tarfile
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from video_management.services import product_image_models as pim
from video_management.services.product_image_service import ProductImageConfigError

CREDENTIALS = {'GOOGLE_DRIVE_CLIENT_ID': 'id', 'GOOGLE_DRIVE_CLIENT_SECRET': 'secret', 'GOOGLE_DRIVE_REFRESH_TOKEN': 'rt'}


class ReadSettingsTests(SimpleTestCase):
    def test_not_enabled_without_folder(self):
        with mock.patch.dict(os.environ, {pim.DRIVE_FOLDER_ENV: ''}, clear=False):
            self.assertIsNone(pim.read_settings())

    def test_missing_variables_are_named(self):
        env = {pim.DRIVE_FOLDER_ENV: 'folder', pim.MODELS_DIR_ENV: '', 'GOOGLE_DRIVE_CLIENT_ID': 'id',
               'GOOGLE_DRIVE_CLIENT_SECRET': '', 'GOOGLE_DRIVE_REFRESH_TOKEN': 'rt'}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(ProductImageConfigError) as ctx:
                pim.read_settings()
        self.assertIn(pim.MODELS_DIR_ENV, str(ctx.exception))
        self.assertIn('GOOGLE_DRIVE_CLIENT_SECRET', str(ctx.exception))
        self.assertNotIn('GOOGLE_DRIVE_CLIENT_ID,', str(ctx.exception))

    def test_complete_settings(self):
        env = {pim.DRIVE_FOLDER_ENV: 'folder', pim.MODELS_DIR_ENV: '/m', **CREDENTIALS}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(pim.read_settings(), ('folder', '/m', ('id', 'secret', 'rt')))


class SyncTests(SimpleTestCase):
    def setUp(self):
        self.target = tempfile.mkdtemp()
        self.drive = [('models/a.onnx', 'id-a', 3), ('pose-library/p1/meta.json', 'id-m', 2)]
        self.content = {'id-a': b'abc', 'id-m': b'{}'}
        patches = [
            mock.patch.object(pim, '_access_token', return_value='token'),
            mock.patch.object(pim, 'list_files', side_effect=lambda token, folder: self.drive),
            mock.patch.object(pim, '_download', side_effect=self._fake_download),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.downloads = []

    def _fake_download(self, token, file_id, dest):
        self.downloads.append(file_id)
        with open(dest, 'wb') as f:
            f.write(self.content[file_id])

    def test_downloads_missing_files_into_subfolders(self):
        count = pim.sync('folder', self.target, ('id', 'secret', 'rt'))
        self.assertEqual(count, 2)
        with open(os.path.join(self.target, 'pose-library/p1/meta.json'), 'rb') as f:
            self.assertEqual(f.read(), b'{}')

    def test_archive_is_extracted_into_folder_and_reextracted_if_missing(self):
        source = tempfile.mkdtemp()
        with open(os.path.join(source, 'index.json'), 'w') as f:
            f.write('{"poses": []}')
        archive = os.path.join(tempfile.mkdtemp(), 'pose-library.tar')
        with tarfile.open(archive, 'w') as tar:
            tar.add(source, arcname='.')
        with open(archive, 'rb') as f:
            self.content['id-t'] = f.read()
        self.drive = [('pose-library.tar', 'id-t', len(self.content['id-t']))]
        pim.sync('folder', self.target, ('id', 'secret', 'rt'))
        index = os.path.join(self.target, 'pose-library', 'index.json')
        self.assertTrue(os.path.isfile(index))
        shutil.rmtree(os.path.join(self.target, 'pose-library'))
        self.downloads.clear()
        self.assertEqual(pim.sync('folder', self.target, ('id', 'secret', 'rt')), 0)
        self.assertEqual(self.downloads, [])
        self.assertTrue(os.path.isfile(index))

    def test_skips_files_with_same_size_and_refreshes_changed(self):
        pim.sync('folder', self.target, ('id', 'secret', 'rt'))
        self.downloads.clear()
        self.drive[0] = ('models/a.onnx', 'id-a', 4)
        self.content['id-a'] = b'abcd'
        count = pim.sync('folder', self.target, ('id', 'secret', 'rt'))
        self.assertEqual((count, self.downloads), (1, ['id-a']))


class EnsureModelsTests(SimpleTestCase):
    def setUp(self):
        pim._synced = False
        self.addCleanup(setattr, pim, '_synced', False)

    def test_noop_when_not_enabled(self):
        with mock.patch.object(pim, 'read_settings', return_value=None), mock.patch.object(pim, 'sync') as sync:
            pim.ensure_models()
        sync.assert_not_called()

    def test_syncs_once_per_process(self):
        settings = ('folder', '/m', ('id', 'secret', 'rt'))
        with mock.patch.object(pim, 'read_settings', return_value=settings), \
                mock.patch.object(pim, 'sync', return_value=0) as sync:
            pim.ensure_models()
            pim.ensure_models()
        sync.assert_called_once_with(*settings)

    def test_network_error_becomes_config_error(self):
        with mock.patch.object(pim, 'read_settings', return_value=('folder', '/m', ('a', 'b', 'c'))), \
                mock.patch.object(pim, 'sync', side_effect=OSError('timed out')):
            with self.assertRaises(ProductImageConfigError) as ctx:
                pim.ensure_models()
        self.assertIn(pim.DRIVE_FOLDER_ENV, str(ctx.exception))
        self.assertFalse(pim._synced)


class BackgroundSyncTests(SimpleTestCase):
    def test_only_under_gunicorn_with_folder(self):
        with mock.patch.object(pim.threading, 'Thread') as thread:
            with mock.patch.object(pim.sys, 'argv', ['manage.py', 'test']), \
                    mock.patch.dict(os.environ, {pim.DRIVE_FOLDER_ENV: 'folder'}):
                pim.start_background_sync()
            thread.assert_not_called()
            with mock.patch.object(pim.sys, 'argv', ['/usr/local/bin/gunicorn']), \
                    mock.patch.dict(os.environ, {pim.DRIVE_FOLDER_ENV: ''}):
                pim.start_background_sync()
            thread.assert_not_called()
            with mock.patch.object(pim.sys, 'argv', ['/usr/local/bin/gunicorn']), \
                    mock.patch.dict(os.environ, {pim.DRIVE_FOLDER_ENV: 'folder'}):
                pim.start_background_sync()
            thread.assert_called_once()
