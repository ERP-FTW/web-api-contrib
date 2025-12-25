# -*- coding: utf-8 -*-
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

"""Customer-side sync of a vendor's customer-specific pricelist snapshot."""

import json
import logging
from urllib.parse import urljoin, urlencode

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None


class ProductPricelist(models.Model):
    """Customer-side timestamp for when a pricelist snapshot was last imported.

    Note: the base workflow imports into product.supplierinfo. This field is
    still useful if you also track the vendor agreement as a product.pricelist.
    """

    _inherit = "product.pricelist"

    api_last_sync_at = fields.Datetime(
        string="API Last Synced At",
        help="Updated when this database syncs a vendor pricelist snapshot.",
    )


class VendorPricelistBackend(models.Model):
    _name = "vendor.pricelist.backend"
    _description = "Vendor Pricelist Backend"

    name = fields.Char(required=True, default="Vendor Pricelist Backend")
    active = fields.Boolean(default=True)

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)

    vendor_partner_id = fields.Many2one(
        "res.partner",
        string="Vendor",
        required=True,
        domain=[("supplier_rank", ">", 0)],
        help="Vendor partner whose prices will be synced into product.supplierinfo.",
    )

    base_url = fields.Char(string="Vendor Base URL", required=True, help="Example: https://vendor.odoo.com")
    endpoint_path = fields.Char(string="Endpoint Path", required=True, default="/api/customer_pricelist")
    api_key = fields.Char(string="API Key", required=True)
    api_header_name = fields.Char(string="API Key Header", default="X-Api-Key", required=True)

    include_inactive = fields.Boolean(default=False)
    only_purchasable = fields.Boolean(default=True)
    create_missing_products = fields.Boolean(default=False)

    local_pricelist_id = fields.Many2one(
        "product.pricelist",
        string="Local Pricelist (Optional)",
        help=(
            "If set, this pricelist will have its 'API Last Synced At' timestamp updated "
            "each time a vendor snapshot is synced."
        ),
    )

    last_sync_at = fields.Datetime(string="Last Sync")
    last_error = fields.Text(string="Last Error")
    last_http_status = fields.Integer(string="Last HTTP Status")
    last_response_snippet = fields.Text(string="Last Response Snippet")

    def action_test_connection(self):
        self.ensure_one()
        _logger.info("Testing vendor pricelist endpoint backend_id=%s", self.id)
        data = self._fetch_payload(limit=1, offset=0)
        count = len((data or {}).get("items") or [])
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Connection OK"), "message": _("Fetched %s item(s).") % count, "sticky": False},
        }

    def action_sync_now(self):
        for backend in self:
            backend._sync()
        return True

    def _cron_sync(self):
        for backend in self.search([("active", "=", True)]):
            backend._sync()

    def _sync(self):
        self.ensure_one()
        try:
            payload = self._fetch_payload()
            items = payload.get("items") or []
            if not isinstance(items, list):
                raise UserError(_("Vendor response missing 'items' list."))

            updated = created = skipped = 0
            for item in items:
                res = self._apply_item(item)
                if res == "updated":
                    updated += 1
                elif res == "created":
                    created += 1
                else:
                    skipped += 1

            self.last_sync_at = fields.Datetime.now()
            self.last_error = False

            # Optional convenience: stamp a local pricelist with the sync time.
            if self.local_pricelist_id:
                self.local_pricelist_id.sudo().write({
                    "api_last_sync_at": self.last_sync_at,
                })
            _logger.info(
                "Vendor pricelist sync done backend_id=%s updated=%s created=%s skipped=%s",
                self.id, updated, created, skipped
            )
        except Exception as exc:
            self.last_error = str(exc)
            _logger.exception("Vendor pricelist sync failed backend_id=%s: %s", self.id, exc)
            raise

    def _fetch_payload(self, limit=2000, offset=0):
        self.ensure_one()

        params = {
            "limit": limit,
            "offset": offset,
            "include_inactive": "true" if self.include_inactive else "false",
            "only_purchasable": "true" if self.only_purchasable else "false",
        }
        url = urljoin(self.base_url.rstrip("/") + "/", self.endpoint_path.lstrip("/"))
        url = url + "?" + urlencode(params)
        headers = {self.api_header_name: self.api_key, "Accept": "application/json"}

        if requests:
            resp = requests.get(url, headers=headers, timeout=60)
            self.last_http_status = resp.status_code
            self.last_response_snippet = (resp.text or "")[:2000]
            if resp.status_code >= 400:
                raise UserError(_("Vendor endpoint HTTP %s: %s") % (resp.status_code, resp.text[:200]))
            return resp.json()

        import urllib.request
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=60) as f:
            body = f.read().decode("utf-8", errors="replace")
            self.last_http_status = getattr(f, "status", 200)
            self.last_response_snippet = body[:2000]
            return json.loads(body)

    def _supplier_partner_field(self):
        """Odoo 18 uses partner_id; older versions used name."""
        SupplierInfo = self.env["product.supplierinfo"]
        if "partner_id" in SupplierInfo._fields:
            return "partner_id"
        if "name" in SupplierInfo._fields:
            return "name"
        raise UserError(_("Cannot find vendor field on product.supplierinfo (expected partner_id or name)."))

    def _parse_for_field(self, model, field_name, value):
        """Parse JSON string into proper Date/Datetime for the target field."""
        if not value:
            return False
        f = model._fields.get(field_name)
        if not f:
            return False

        # Payload may contain "YYYY-MM-DD HH:MM:SS" strings.
        try:
            if f.type == "datetime":
                return fields.Datetime.from_string(value)
            if f.type == "date":
                # Accept both "YYYY-MM-DD" and "YYYY-MM-DD HH:MM:SS"
                return fields.Date.from_string(value[:10])
        except Exception:
            _logger.debug("Could not parse %s=%r for %s", field_name, value, model._name)
        return False

    # -------------------------
    # Patch this method
    # -------------------------

    def _apply_item(self, item):
        """Apply one vendor item to local product + supplierinfo."""
        self.ensure_one()

        SupplierInfo = self.env["product.supplierinfo"].sudo()
        Product = self.env["product.product"].sudo()
        ProductTmpl = self.env["product.template"].sudo()

        vendor_partner_field = self._supplier_partner_field()

        vendor_partner = self.vendor_partner_id
        if not vendor_partner:
            raise UserError(_("Vendor Partner is missing on the backend configuration."))

        # --- locate or create product ---
        default_code = item.get("default_code") or False
        barcode = item.get("barcode") or False
        vendor_product_id = item.get("vendor_product_id") or False
        name = item.get("name") or _("Vendor Product")

        product = False
        if barcode:
            product = Product.search([("barcode", "=", barcode)], limit=1)
        if not product and default_code:
            product = Product.search([("default_code", "=", default_code)], limit=1)

        if not product and self.create_missing_products:
            tmpl_vals = {"name": name, "purchase_ok": True, "sale_ok": False}
            if default_code:
                # default_code is on product.product; we set it after variant exists
                pass
            tmpl = ProductTmpl.create(tmpl_vals)
            product = tmpl.product_variant_id
            if default_code:
                product.default_code = default_code
            if barcode:
                product.barcode = barcode

        if not product:
            raise UserError(_("Could not match product and 'Create Missing Products' is disabled. Item=%s") % item)

        # --- find existing supplierinfo line for this vendor + product/template ---
        # Prefer product-specific line; fallback to template line
        domain = [(vendor_partner_field, "=", vendor_partner.id)]
        if "product_id" in SupplierInfo._fields:
            domain_product = domain + [("product_id", "=", product.id)]
            supplierinfo = SupplierInfo.search(domain_product, limit=1)
        else:
            supplierinfo = False

        if not supplierinfo and "product_tmpl_id" in SupplierInfo._fields:
            domain_tmpl = domain + [("product_tmpl_id", "=", product.product_tmpl_id.id)]
            supplierinfo = SupplierInfo.search(domain_tmpl, limit=1)

        # --- build vals from payload ---
        price = (item.get("price") or {}).get("value")
        start_date = (item.get("price") or {}).get("start_date")
        end_date = (item.get("price") or {}).get("end_date")

        vals = {}

        # vendor
        vals[vendor_partner_field] = vendor_partner.id

        # link to product/template
        if "product_id" in SupplierInfo._fields:
            vals["product_id"] = product.id
        if "product_tmpl_id" in SupplierInfo._fields:
            vals["product_tmpl_id"] = product.product_tmpl_id.id

        # price field name differs by version/customization; handle common ones
        if "price" in SupplierInfo._fields:
            vals["price"] = float(price or 0.0)
        elif "price_unit" in SupplierInfo._fields:
            vals["price_unit"] = float(price or 0.0)

        # min qty (your payload might add this later)
        if "min_qty" in SupplierInfo._fields and item.get("min_qty"):
            vals["min_qty"] = float(item["min_qty"])

        # date range from vendor pricelist rule
        if "date_start" in SupplierInfo._fields:
            parsed = self._parse_for_field(SupplierInfo, "date_start", start_date)
            if parsed:
                vals["date_start"] = parsed
        if "date_end" in SupplierInfo._fields:
            parsed = self._parse_for_field(SupplierInfo, "date_end", end_date)
            if parsed:
                vals["date_end"] = parsed

        # purchase uom (optional)
        purchase_uom = (item.get("purchase_uom") or {}).get("id")
        if purchase_uom:
            if "product_uom" in SupplierInfo._fields:
                vals["product_uom"] = purchase_uom
            elif "product_uom_id" in SupplierInfo._fields:
                vals["product_uom_id"] = purchase_uom

        # --- upsert supplierinfo ---
        if supplierinfo:
            supplierinfo.write(vals)
            action = "updated"
        else:
            supplierinfo = SupplierInfo.create(vals)
            action = "created"

        return {"product_id": product.id, "supplierinfo_id": supplierinfo.id, "action": action}
    def _find_or_create_product(self, barcode=False, default_code=False, name=False):
        Product = self.env["product.product"].with_context(active_test=False)
        product = False

        if barcode:
            product = Product.search([("barcode", "=", barcode)], limit=1)
        if not product and default_code:
            product = Product.search([("default_code", "=", default_code)], limit=1)
        if not product and name:
            product = Product.search([("name", "=", name)], limit=1)

        if product:
            return product

        if not self.create_missing_products:
            return False

        tmpl_vals = {
            "name": name or default_code or barcode or _("New Product"),
            "default_code": default_code,
            "barcode": barcode,
            "purchase_ok": True,
            "sale_ok": False,
            "company_id": self.company_id.id,
        }
        tmpl = self.env["product.template"].create(tmpl_vals)
        _logger.info("Created product template id=%s name=%s", tmpl.id, tmpl.display_name)
        return tmpl.product_variant_id
