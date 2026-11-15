from types import SimpleNamespace

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCustomerPricelistExport(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mixin = cls.env["endpoint.mixin"]
        cls.product = cls.env["product.product"].create(
            {
                "name": "Export Product",
                "default_code": "EXP-001",
                "barcode": "990000000001",
                "list_price": 100.0,
            }
        )
        cls.pricelist = cls.env["product.pricelist"].create(
            {
                "name": "Customer Contract",
                "currency_id": cls.env.company.currency_id.id,
            }
        )
        cls.rule = cls.env["product.pricelist.item"].create(
            {
                "pricelist_id": cls.pricelist.id,
                "applied_on": "1_product",
                "product_tmpl_id": cls.product.product_tmpl_id.id,
                "compute_price": "fixed",
                "fixed_price": 42.5,
                "min_quantity": 1.0,
            }
        )
        cls.env["product.pricelist.item"].create(
            {
                "pricelist_id": cls.pricelist.id,
                "applied_on": "3_global",
                "compute_price": "fixed",
                "fixed_price": 10.0,
            }
        )
        for index in range(3):
            cls.env["product.product"].create(
                {
                    "name": f"Paged Export Product {index}",
                    "default_code": f"PAGE-{index}",
                    "purchase_ok": True,
                }
            )
        cls.partner = cls.env["res.partner"].create(
            {"name": "Synthetic Catalog Customer"}
        )
        cls.endpoint = cls.env["endpoint.endpoint"].create(
            {
                "name": "Synthetic Customer Pricelist Export",
                "route": "/synthetic/customer-prices",
                "request_method": "GET",
                "route_type": "json",
                "exec_mode": "customer_pricelist_export",
                "catalog_partner_id": cls.partner.id,
                "catalog_pricelist_id": cls.pricelist.id,
            }
        )

    def test_odoo_19_keyword_price_api_returns_contract_price(self):
        price, rule_id, min_qty = self.mixin._compute_pricelist_price(
            self.pricelist,
            self.product,
            self.env.company.partner_id,
            qty=1.0,
            uom=self.product.uom_id,
        )
        self.assertEqual(price, 42.5)
        self.assertEqual(rule_id, self.rule.id)
        self.assertEqual(min_qty, 1.0)
        self.assertNotEqual(price, self.product.list_price)

    def test_pricing_api_failure_never_falls_back_to_list_price(self):
        broken_pricelist = SimpleNamespace(
            currency_id=self.env.company.currency_id,
            _get_product_price_rule=lambda *args, **kwargs: (_ for _ in ()).throw(
                TypeError("keyword-only regression specimen")
            ),
        )
        with self.assertRaisesRegex(UserError, "Could not compute"):
            self.mixin._compute_pricelist_price(
                broken_pricelist,
                self.product,
                self.env.company.partner_id,
                uom=self.product.uom_id,
            )

    def test_serialization_uses_stable_uom_and_currency_identity(self):
        item = self.mixin._serialize_pricelist_item(
            self.product, self.env.company.partner_id, self.pricelist
        )
        self.assertTrue(item["purchase_uom"]["external_id"])
        self.assertEqual(
            item["price"]["currency"]["code"], self.pricelist.currency_id.name
        )
        self.assertIn("id", item["purchase_uom"])
        self.assertIn("id", item["price"]["currency"])

    def test_page_parameters_are_bounded(self):
        self.assertEqual(self.mixin._bounded_page_limit(-1), 2000)
        self.assertEqual(self.mixin._bounded_page_limit(0), 2000)
        self.assertEqual(self.mixin._bounded_page_limit(5), 5)
        self.assertEqual(self.mixin._bounded_page_limit(99999), 2000)

    def test_id_ordered_pages_are_stable_and_non_overlapping(self):
        first = self.endpoint._handle_exec__customer_pricelist_export(
            SimpleNamespace(params={"limit": 2, "offset": 0})
        )["payload"]
        second = self.endpoint._handle_exec__customer_pricelist_export(
            SimpleNamespace(params={"limit": 2, "offset": 2})
        )["payload"]
        first_ids = [item["vendor_product_id"] for item in first["items"]]
        second_ids = [item["vendor_product_id"] for item in second["items"]]
        self.assertEqual(first_ids, sorted(first_ids))
        self.assertEqual(second_ids, sorted(second_ids))
        self.assertFalse(set(first_ids) & set(second_ids))
        self.assertEqual(first["meta"]["next_offset"], 2)
        self.assertEqual(first["meta"]["total_count"], second["meta"]["total_count"])

    def test_missing_customer_fails_validation(self):
        endpoint = self.endpoint.copy(
            {"name": "Missing Customer", "route": "/synthetic/missing-customer"}
        )
        endpoint.catalog_partner_id = False
        with self.assertRaisesRegex(UserError, "Missing Catalog Customer"):
            endpoint._validate_exec__customer_pricelist_export()

    def test_missing_explicit_and_customer_pricelist_fails_closed(self):
        partner = self.env["res.partner"].create(
            {"name": "Synthetic Customer Without Pricelist"}
        )
        partner.property_product_pricelist = False
        self.assertFalse(partner.property_product_pricelist)
        with self.assertRaisesRegex(UserError, "No pricelist found"):
            self.endpoint.copy(
                {
                    "name": "Missing Pricelist",
                    "route": "/synthetic/missing-pricelist",
                    "catalog_partner_id": partner.id,
                    "catalog_pricelist_id": False,
                }
            )
