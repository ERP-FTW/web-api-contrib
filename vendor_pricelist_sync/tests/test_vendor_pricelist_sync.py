from types import SimpleNamespace
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestVendorPricelistSync(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.env["res.partner"].create(
            {"name": "Synthetic Vendor", "supplier_rank": 1}
        )
        cls.backend = cls.env["vendor.pricelist.backend"].create(
            {
                "name": "Synthetic Backend",
                "vendor_partner_id": cls.vendor.id,
                "base_url": "https://sandbox.invalid",
                "endpoint_path": "/prices",
                "api_key": "test-only",
                "page_size": 2,
            }
        )
        cls.products = cls.env["product.product"]
        for index in range(3):
            cls.products |= cls.env["product.product"].create(
                {
                    "name": f"Sync Product {index}",
                    "default_code": f"SYNC-{index}",
                    "barcode": f"99100000000{index}",
                }
            )
        cls.uom = cls.products[0].uom_id
        cls.uom_external_id = cls.uom.get_external_id()[cls.uom.id]
        cls.currency = cls.env.company.currency_id

    def _item(self, product, price=12.5):
        return {
            "vendor_product_id": 987654321,
            "default_code": product.default_code,
            "barcode": product.barcode,
            "name": product.display_name,
            "purchase_uom": {
                "id": 987654321,
                "external_id": self.uom_external_id,
                "name": self.uom.name,
                "reference_external_id": self.uom_external_id,
            },
            "price": {
                "value": price,
                "currency": {
                    "id": 987654321,
                    "code": self.currency.name,
                    "name": self.currency.name,
                },
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
            "min_qty": 3.0,
        }

    def _pages(self):
        items = [self._item(product) for product in self.products]
        return {
            0: {
                "items": items[:2],
                "meta": {
                    "offset": 0,
                    "limit": 2,
                    "count": 2,
                    "total_count": 3,
                    "has_more": True,
                    "next_offset": 2,
                },
            },
            2: {
                "items": items[2:],
                "meta": {
                    "offset": 2,
                    "limit": 2,
                    "count": 1,
                    "total_count": 3,
                    "has_more": False,
                    "next_offset": False,
                },
            },
        }

    def test_upsert_maps_stable_identities_and_is_idempotent(self):
        item = self._item(self.products[0])
        first = self.backend._apply_item(item)
        second = self.backend._apply_item({**item, "price": {**item["price"], "value": 15.0}})
        self.assertEqual(first["action"], "created")
        self.assertEqual(second["action"], "updated")

        lines = self.env["product.supplierinfo"].search(
            [
                ("partner_id", "=", self.vendor.id),
                ("product_id", "=", self.products[0].id),
            ]
        )
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.price, 15.0)
        self.assertEqual(lines.min_qty, 3.0)
        self.assertEqual(lines.product_uom_id, self.uom)
        self.assertEqual(lines.currency_id, self.currency)
        self.assertEqual(str(lines.date_start), "2026-01-01")
        self.assertEqual(str(lines.date_end), "2026-12-31")
        self.assertNotEqual(lines.product_uom_id.id, item["purchase_uom"]["id"])
        self.assertNotEqual(lines.currency_id.id, item["price"]["currency"]["id"])

    def test_complete_pagination_and_reconciliation(self):
        pages = self._pages()

        def fetch(_recordset, limit=2000, offset=0):
            self.assertEqual(limit, 2)
            return pages[offset]

        with patch.object(type(self.backend), "_fetch_payload", autospec=True, side_effect=fetch):
            first = self.backend._sync()
            second = self.backend._sync()

        self.assertEqual(
            first,
            {"exported_count": 3, "created": 3, "updated": 0, "skipped_or_rejected": 0},
        )
        self.assertEqual(
            second,
            {"exported_count": 3, "created": 0, "updated": 3, "skipped_or_rejected": 0},
        )
        self.assertEqual(
            first["exported_count"],
            first["created"] + first["updated"] + first["skipped_or_rejected"],
        )
        self.assertEqual(self.backend.last_exported_count, 3)

    def test_barcode_has_priority_over_sku(self):
        item = self._item(self.products[0])
        item["default_code"] = self.products[1].default_code
        result = self.backend._apply_item(item)
        self.assertEqual(result["product_id"], self.products[0].id)

    def test_ambiguous_sku_fails_closed(self):
        duplicates = self.env["product.product"]
        for index in range(2):
            duplicates |= self.env["product.product"].create(
                {"name": f"Duplicate {index}", "default_code": "DUPLICATE-SKU"}
            )
        item = self._item(duplicates[0])
        item["barcode"] = False
        with self.assertRaisesRegex(UserError, "Ambiguous product"):
            self.backend._apply_item(item)

    def test_unmapped_uom_and_currency_fail_closed(self):
        bad_uom = self._item(self.products[0])
        bad_uom["purchase_uom"] = {"external_id": "uom.does_not_exist"}
        with self.assertRaisesRegex(UserError, "Unknown purchase UoM"):
            self.backend._apply_item(bad_uom)

        bad_currency = self._item(self.products[0])
        bad_currency["price"]["currency"]["code"] = "ZZZ"
        with self.assertRaisesRegex(UserError, "Currency code"):
            self.backend._apply_item(bad_currency)

    def test_repeated_page_rolls_back_partial_application(self):
        page = self._pages()[0]
        page = {"items": page["items"], "meta": {**page["meta"], "total_count": 4}}
        repeated = {
            "items": page["items"],
            "meta": {
                "offset": 2,
                "limit": 2,
                "count": 2,
                "total_count": 4,
                "has_more": False,
                "next_offset": False,
            },
        }
        before = self.env["product.supplierinfo"].search_count(
            [("partner_id", "=", self.vendor.id)]
        )

        def fetch(_recordset, limit=2000, offset=0):
            return page if offset == 0 else repeated

        with patch.object(type(self.backend), "_fetch_payload", autospec=True, side_effect=fetch):
            with self.assertRaisesRegex(UserError, "repeated"):
                self.backend._sync()

        after = self.env["product.supplierinfo"].search_count(
            [("partner_id", "=", self.vendor.id)]
        )
        self.assertEqual(after, before)

    def test_malformed_payload_fails_before_updates(self):
        with patch.object(
            type(self.backend),
            "_fetch_payload",
            autospec=True,
            return_value={"items": "not-a-list", "meta": {}},
        ):
            with self.assertRaisesRegex(UserError, "items"):
                self.backend._sync()

    def test_http_auth_failure_is_reported_without_application(self):
        response = SimpleNamespace(status_code=401, text="synthetic unauthorized")
        with patch(
            "odoo.addons.vendor_pricelist_sync.models.vendor_pricelist_backend.requests.get",
            return_value=response,
        ):
            with self.assertRaisesRegex(UserError, "HTTP 401"):
                self.backend._fetch_payload(limit=1, offset=0)

        self.assertEqual(
            self.env["product.supplierinfo"].search_count(
                [("partner_id", "=", self.vendor.id)]
            ),
            0,
        )
