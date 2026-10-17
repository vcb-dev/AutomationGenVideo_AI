"""Dựng thư viện tư thế chị Nhạm cầm SP (xem services/pose_library.py) từ một thư mục ảnh media.

  python manage.py build_pose_library <thư mục ảnh> <thư mục thư viện> [--exclude tên-file ...]

Chạy lại được (ảnh đã dựng thì bỏ qua). Cần biến model của held_swap; mỗi ảnh ~5–10s CPU."""
import hashlib
import json
import os

from django.core.management.base import BaseCommand, CommandError

from video_management.services import held_swap as hs
from video_management.services import pose_library as pl
from video_management.services import product_image_service as svc

IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')


class Command(BaseCommand):
    help = 'Dựng thư viện tư thế chị Nhạm cầm SP cho chế độ tự chọn tư thế (0đ).'

    def add_arguments(self, parser):
        parser.add_argument('source', help='Thư mục ảnh chị Nhạm cầm SP')
        parser.add_argument('target', help='Thư mục thư viện tư thế (ghi index.json + mỗi tư thế một thư mục con)')
        parser.add_argument('--exclude', nargs='*', default=[], help='Tên file bỏ qua (vd ảnh không phải chị Nhạm)')

    def handle(self, *args, **options):
        source, target = options['source'], options['target']
        if not os.path.isdir(source):
            raise CommandError(f'Không có thư mục ảnh {source}')
        os.makedirs(target, exist_ok=True)
        try:
            models = hs.get_models(*hs.read_swap_paths())
        except svc.ProductImageConfigError as e:
            raise CommandError(str(e))
        excluded = set(options['exclude'])
        metas = []
        index_path = os.path.join(target, pl.INDEX_FILE)
        known = set()
        if os.path.isfile(index_path):
            with open(index_path, encoding='utf-8') as f:
                known = set(json.load(f)['poses'])
        names = sorted(n for n in os.listdir(source) if n.lower().endswith(IMAGE_EXTENSIONS) and n not in excluded)
        for i, name in enumerate(names, 1):
            data = open(os.path.join(source, name), 'rb').read()
            pose_id = hashlib.sha1(data).hexdigest()[:12]
            meta_path = os.path.join(target, pose_id, pl.META_FILE)
            if pose_id in known and os.path.isfile(meta_path):
                with open(meta_path, encoding='utf-8') as f:
                    metas.append(json.load(f))
                continue
            try:
                scene = hs.prepare_scene(models, svc.open_upright_image(data, name))
            except svc.ProductImageInputError as e:
                self.stdout.write(f'[{i}/{len(names)}] bỏ {name}: {e}')
                continue
            ok, reason = pl.pose_usable(scene)
            if not ok:
                self.stdout.write(f'[{i}/{len(names)}] bỏ {name}: {reason}')
                continue
            metas.append(pl.save_pose(scene, target, pose_id, name))
            pl.write_index(target, metas)
            self.stdout.write(f'[{i}/{len(names)}] {name} → {pose_id}')
        pl.write_index(target, metas)
        self.stdout.write(self.style.SUCCESS(f'Thư viện {target}: {len(metas)} tư thế.'))
