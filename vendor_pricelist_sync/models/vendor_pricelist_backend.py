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
    page_size = fields.Integer(default=2000, required=True)

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
    last_exported_count = fields.Integer(readonly=True)
    last_created_count = fields.Integer(readonly=True)
    last_updated_count = fields.Integer(readonly=True)
    last_skipped_count = fields.Integer(readonly=True)

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
            with self.env.cr.savepoint():
                summary = self._sync_all_pages()
                self.write(
                    {
                        "last_sync_at": fields.Datetime.now(),
                        "last_error": False,
                        "last_exported_count": summary["exported_count"],
                        "last_created_count": summary["created"],
                        "last_updated_count": summary["updated"],
                        "last_skipped_count": summary["skipped_or_rejected"],
                    }
                )

                if self.local_pricelist_id:
                    self.local_pricelist_id.sudo().write(
                        {"api_last_sync_at": self.last_sync_at}
                    )
                _logger.info(
                    "Vendor pricelist sync done backend_id=%s exported=%s updated=%s created=%s skipped=%s",
                    self.id,
                    summary["exported_count"],
                    summary["updated"],
                    summary["created"],
                    summary["skipped_or_rejected"],
                )
                return summary
        except Exception as exc:
            self.last_error = str(exc)
            _logger.exception("Vendor pricelist sync failed backend_id=%s: %s", self.id, exc)
            raise

    def _sync_all_pages(self):
        page_size = int(self.page_size or 0)
        if page_size <= 0 or page_size > 2000:
            raise UserError(_("Page Size must be between 1 and 2000."))

        offset = 0
        expected_total = None
        seen_page_keys = set()
        counts = {"created": 0, "updated": 0, "skipped_or_rejected": 0}

        while True:
            payload = self._fetch_payload(limit=page_size, offset=offset)
            items = payload.get("items")
            meta = payload.get("meta")
            if not isinstance(items, list) or not isinstance(meta, dict):
                raise UserError(_("Vendor response must contain 'items' list and 'meta' object."))

            try:
                response_offset = int(meta["offset"])
                response_limit = int(meta["limit"])
                response_count = int(meta["count"])
                total_count = int(meta["total_count"])
            except (KeyError, TypeError, ValueError) as exc:
                raise UserError(_("Vendor response contains invalid pagination metadata.")) from exc

            if response_offset != offset or response_limit != page_size:
                raise UserError(_("Vendor response pagination does not match the requested page."))
            if response_count != len(items) or response_count > response_limit:
                raise UserError(_("Vendor response pagination count is inconsistent."))
            if total_count < response_offset + response_count:
                raise UserError(_("Vendor response total_count is inconsistent."))
            if expected_total is None:
                expected_total = total_count
            elif expected_total != total_count:
                raise UserError(_("Vendor response total_count changed during pagination."))

            page_key = tuple(
                (item.get("barcode"), item.get("default_code"), item.get("vendor_product_id"))
                if isinstance(item, dict)
                else (repr(item),)
                for item in items
            )
            if items and page_key in seen_page_keys:
                raise UserError(_("Vendor response repeated a previously consumed page."))
            seen_page_keys.add(page_key)

            for item in items:
                result = self._apply_item(item)
                action = result.get("action") if isinstance(result, dict) else False
                if action not in counts:
                    raise UserError(_("Vendor item returned an unknown reconciliation action."))
                counts[action] += 1

            has_more = bool(meta.get("has_more"))
            next_offset = meta.get("next_offset")
            if has_more:
                try:
                    next_offset = int(next_offset)
                except (TypeError, ValueError) as exc:
                    raise UserError(_("Vendor response next_offset is invalid.")) from exc
                if not items or next_offset != offset + len(items) or next_offset <= offset:
                    raise UserError(_("Vendor response pagination did not advance deterministically."))
                offset = next_offset
                continue
            if next_offset not in (False, None):
                raise UserError(_("Vendor response has next_offset while has_more is false."))
            break

        exported_count = sum(counts.values())
        if exported_count != expected_total:
            raise UserError(
                _("Vendor response reconciliation failed: exported %s of %s items.")
                % (exported_count, expected_total)
            )
        return {"exported_count": exported_count, **counts}

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
            data = resp.json()
            return self._normalize_payload(data)

        import urllib.request
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=60) as f:
            body = f.read().decode("utf-8", errors="replace")
            self.last_http_status = getattr(f, "status", 200)
            self.last_response_snippet = body[:2000]
            return self._normalize_payload(json.loads(body))

    def _normalize_payload(self, data):
        if not isinstance(data, dict):
            raise UserError(_("Vendor response must be a JSON object."))
        payload = data.get("payload", data)
        if not isinstance(payload, dict):
            raise UserError(_("Vendor response 'payload' must be an object."))
        return payload

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

    def _find_product(self, item):
        Product = self.env["product.product"].sudo().with_context(active_test=False)
        barcode = item.get("barcode") or False
        default_code = item.get("default_code") or False

        for field_name, value in (("barcode", barcode), ("default_code", default_code)):
            if not value:
                continue
            matches = Product.search([(field_name, "=", value)], limit=2)
            if len(matches) > 1:
                raise UserError(
                    _("Ambiguous product %s %r; refusing to select an arbitrary record.")
                    % (field_name, value)
                )
            if matches:
                return matches

        if not self.create_missing_products:
            raise UserError(
                _("Could not match product by barcode or SKU and product creation is disabled.")
            )

        template = self.env["product.template"].sudo().create(
            {
                "name": item.get("name") or default_code or barcode or _("Vendor Product"),
                "purchase_ok": True,
                "sale_ok": False,
                "company_id": self.company_id.id,
            }
        )
        product = template.product_variant_id
        product.write({"default_code": default_code, "barcode": barcode})
        return product

    def _resolve_uom(self, item, product):
        uom_data = item.get("purchase_uom")
        if not isinstance(uom_data, dict):
            raise UserError(_("Vendor item is missing purchase_uom mapping data."))

        uom = self.env["uom.uom"]
        external_id = uom_data.get("external_id")
        if external_id:
            uom = self.env.ref(external_id, raise_if_not_found=False)
            if not uom or uom._name != "uom.uom":
                raise UserError(_("Unknown purchase UoM external ID %s.") % external_id)
        else:
            name = uom_data.get("name")
            reference_external_id = uom_data.get("reference_external_id")
            reference_uom = self.env.ref(
                reference_external_id, raise_if_not_found=False
            ) if reference_external_id else False
            if not name or not reference_uom or reference_uom._name != "uom.uom":
                raise UserError(
                    _("Purchase UoM requires an external ID or name/reference fallback.")
                )
            matches = self.env["uom.uom"].search([("name", "=", name)]).filtered(
                lambda candidate: candidate._has_common_reference(reference_uom)
            )
            if len(matches) != 1:
                raise UserError(_("Purchase UoM fallback is missing or ambiguous."))
            uom = matches

        if not uom._has_common_reference(product.uom_id):
            raise UserError(_("Purchase UoM is incompatible with the matched product."))
        return uom

    def _resolve_currency(self, item):
        price_data = item.get("price")
        currency_data = price_data.get("currency") if isinstance(price_data, dict) else None
        code = currency_data.get("code") if isinstance(currency_data, dict) else None
        if not code:
            raise UserError(_("Vendor item is missing an ISO currency code."))
        currencies = self.env["res.currency"].with_context(active_test=False).search(
            [("name", "=", code)], limit=2
        )
        if len(currencies) != 1:
            raise UserError(_("Currency code %s is missing or ambiguous.") % code)
        return currencies

    def _apply_item(self, item):
        """Apply one classified item without using any remote database ID."""
        self.ensure_one()
        if not isinstance(item, dict):
            raise UserError(_("Vendor item must be a JSON object."))
        if not self.vendor_partner_id:
            raise UserError(_("Vendor Partner is missing on the backend configuration."))

        SupplierInfo = self.env["product.supplierinfo"].sudo()
        vendor_partner_field = self._supplier_partner_field()
        product = self._find_product(item)
        uom = self._resolve_uom(item, product)
        currency = self._resolve_currency(item)

        price_data = item.get("price")
        if not isinstance(price_data, dict) or price_data.get("value") is None:
            raise UserError(_("Vendor item is missing a price value."))
        try:
            price = float(price_data["value"])
            min_qty = float(item.get("min_qty") or 0.0)
        except (TypeError, ValueError) as exc:
            raise UserError(_("Vendor item price or minimum quantity is invalid.")) from exc

        start_date = self._parse_for_field(SupplierInfo, "date_start", price_data.get("start_date"))
        end_date = self._parse_for_field(SupplierInfo, "date_end", price_data.get("end_date"))
        vals = {
            vendor_partner_field: self.vendor_partner_id.id,
            "product_id": product.id,
            "product_tmpl_id": product.product_tmpl_id.id,
            "company_id": self.company_id.id,
            "product_uom_id": uom.id,
            "currency_id": currency.id,
            "price": price,
            "min_qty": min_qty,
            "date_start": start_date,
            "date_end": end_date,
        }

        identity_fields = (
            vendor_partner_field,
            "product_id",
            "company_id",
            "product_uom_id",
            "currency_id",
            "min_qty",
            "date_start",
            "date_end",
        )
        domain = [(field_name, "=", vals[field_name]) for field_name in identity_fields]
        matches = SupplierInfo.search(domain, limit=2)
        if len(matches) > 1:
            raise UserError(_("Supplier price identity is ambiguous; refusing an arbitrary update."))

        if matches:
            matches.write(vals)
            supplierinfo = matches
            action = "updated"
        else:
            supplierinfo = SupplierInfo.create(vals)
            action = "created"

        return {
            "product_id": product.id,
            "supplierinfo_id": supplierinfo.id,
            "action": action,
        }
