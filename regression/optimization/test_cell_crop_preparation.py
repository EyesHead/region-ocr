"""Prepared table ink must preserve physical crop geometry and source pixels."""
from pathlib import Path
import sys
import unittest

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import docling_pdf_cell_crop as crops


class PreparedCellCropTests(unittest.TestCase):
    def prepare(self, image):
        helper = getattr(crops, 'prepare_cell_content_ink', None)
        self.assertTrue(callable(helper), 'Table ink preparation must be available')
        return helper(image)

    def source(self):
        image = Image.new('RGB', (120, 90), (235, 235, 235))
        draw = ImageDraw.Draw(image)
        draw.rectangle((9, 10, 13, 60), fill=(230, 230, 230))
        draw.rectangle((10, 8, 60, 11), fill=(230, 230, 230))
        draw.rectangle((58, 10, 62, 60), fill=(230, 230, 230))
        draw.rectangle((10, 58, 60, 62), fill=(230, 230, 230))
        draw.text((22, 25), 'P=42', fill='black')
        return image

    def test_pale_rules_keep_exact_scaled_bounds_and_proof(self):
        with self.source() as image:
            ink = self.prepare(image)
            actual = crops.cell_content_crop(image, [10, 20, 70, 65], [15, 25, 40, 50], ink=ink)
        self.assertEqual(actual, ((15, 13, 57, 57), [17.5, 26.5, 38.5, 48.5], {
            'method': 'physical_cell_interior_excluding_measured_edge_rules',
            'source_cell_bbox': [15, 25, 40, 50],
            'source_pixel_bbox': [15, 13, 57, 57],
            'source_content_bbox': [17.5, 26.5, 38.5, 48.5],
            'minimum_inset_pixels': 3,
            'edge_insets_pixels': [5, 3, 3, 3],
            'edge_rule_pixel_runs': [[9, 14], [8, 12], [58, 63], [58, 63]],
        }))

    def test_one_readonly_mask_serves_adjacent_cells_without_mutating_pixels(self):
        with self.source() as image:
            original = image.tobytes()
            ink = self.prepare(image)
            self.assertFalse(ink.flags.writeable)
            with self.assertRaises(ValueError):
                ink[30, 30] = False
            frozen = ink.tobytes()
            crops.cell_content_crop(image, [10, 20, 70, 65], [15, 25, 40, 50], ink=ink)
            adjacent = crops.cell_content_crop(image, [10, 20, 70, 65], [40, 25, 65, 50], ink=ink)
            self.assertEqual(adjacent[0], (64, 13, 107, 57))
            self.assertEqual(adjacent[1], [42.0, 26.5, 63.5, 48.5])
            self.assertEqual(adjacent[2]['edge_rule_pixel_runs'], [[58, 63], None, None, None])
            self.assertEqual(ink.tobytes(), frozen)
            self.assertEqual(image.tobytes(), original)

    def test_picture_bounds_keep_floor_and_ceil_inside_measured_cell(self):
        with self.source() as image:
            ink = self.prepare(image)
            actual = crops.cell_content_crop(image, [10, 20, 70, 65], [15, 25, 40, 50],
                [18.2, 28.2, 35.2, 40.2], ink=ink)
        self.assertEqual(actual[0], (16, 16, 51, 41))
        self.assertEqual(actual[1], [18.0, 28.0, 35.5, 40.5])

    def test_prepared_mask_must_match_source_image_size(self):
        with Image.new('RGB', (120, 90), 'white') as image:
            ink = self.prepare(image)
            with Image.new('RGB', (90, 120), 'white') as other:
                with self.assertRaises(ValueError):
                    crops.cell_content_crop(other, [0, 0, 90, 120], [0, 0, 90, 120], ink=ink)

    def test_unprepared_call_keeps_three_pixel_inset_and_empty_crop_behavior(self):
        with Image.new('L', (120, 90), 235) as image:
            self.assertEqual(crops.cell_content_crop(image, [0, 0, 120, 90], [0, 0, 120, 90])[0],
                (3, 3, 117, 87))
            self.assertIsNone(crops.cell_content_crop(image, [0, 0, 120, 90], [0, 0, 6, 6]))
            self.assertIsNone(crops.cell_content_crop(image, [0, 0, 0, 90], [0, 0, 120, 90]))


if __name__ == '__main__':
    unittest.main()
