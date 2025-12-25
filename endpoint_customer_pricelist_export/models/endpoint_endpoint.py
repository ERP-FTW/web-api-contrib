# -*- coding: utf-8 -*-
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

"""Vendor-side endpoint: export a *resolved* pricelist snapshot for a specific customer.

Key goal: keep this simple + aligned with Odoo purchasing.
- Vendor publishes a JSON "price snapshot" for the customer (contract pricelist).
- Customer imports into product.supplierinfo.
- Standard Odoo Purchase flows then "just work".

Important simplification:
- Product Assortment is OPTIONAL.
  We DO NOT require configuring assortments (avoids ir.filters/model_id validation issues).
  If an assortment is set, we try to apply it; if it errors, we ignore it and still export.

Query params:
  - limit (default 2000)
  - offset (default 0)
  - include_inactive (default false)
  - only_purchasable (default true)
"""

import logging
from datetime import date, datetime

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class EndpointEndpoint(models.Model):
    _inherit = "endpoint.endpoint"

    catalog_partner_id = fields.Many2one(
        "res.partner",
        string="Catalog Customer",
        help="Customer consuming this export; used to compute customer-specific prices.",
    )
    catalog_pricelist_id = fields.Many2one(
        "product.pricelist",
        string="Catalog Pricelist",
        help="Optional override. If empty, uses the customer's property_product_pricelist.",
    )


class EndpointMixin(models.AbstractModel):
    _inherit = "endpoint.mixin"

    def _selection_exec_mode(self):
        return super()._selection_exec_mode() + [
            ("customer_pricelist_export", "Customer Pricelist Export (Resolved)"),
        ]

    def _validate_exec__customer_pricelist_export(self):
        self.ensure_one()

        if not getattr(self, "catalog_partner_id", False):
            raise UserError(
                _("Missing Catalog Customer. Set 'Catalog Customer' to compute customer-specific pricing.")
            )

        pricelist = self._get_catalog_pricelist()
        if not pricelist:
            raise UserError(
                _(
                    "No pricelist found. Set 'Catalog Pricelist' on the endpoint "
                    "or configure a pricelist on the customer (property_product_pricelist)."
                )
            )

        if getattr(self, "route_type", None) and self.route_type != "json":
            _logger.warning(
                "Endpoint %s route_type is %s (expected 'json'). The handler returns a dict for JSON responses.",
                self.display_name,
                self.route_type,
            )

    def _handle_exec__customer_pricelist_export(self, request):
        self.ensure_one()

        partner = self.catalog_partner_id
        pricelist = self._get_catalog_pricelist()

        params = {}
        try:
            params = dict(getattr(request, "params", {}) or {})
        except Exception:
            params = {}

        limit = self._safe_int(params.get("limit"), default=0) or 2000
        offset = self._safe_int(params.get("offset"), default=0)
        include_inactive = self._safe_bool(params.get("include_inactive"), default=False)
        only_purchasable = self._safe_bool(params.get("only_purchasable"), default=True)

        _logger.info(
            "Customer pricelist export called: endpoint_id=%s route=%s partner_id=%s pricelist_id=%s limit=%s offset=%s",
            self.id,
            getattr(self, "route", ""),
            partner.id,
            pricelist.id if pricelist else False,
            limit,
            offset,
        )

        # Simple domain: no assortment required.
        domain = []
        if only_purchasable:
            domain.append(("purchase_ok", "=", True))

        # Optional: apply assortment if configured, but never fail export because of it.
        if getattr(self, "product_assortment_id", False):
            try:
                assortment_domain = self.product_assortment_id._get_eval_domain()
                if assortment_domain:
                    domain += list(assortment_domain)
            except Exception as exc:
                _logger.warning(
                    "Ignoring product assortment domain error on endpoint %s: %s",
                    self.display_name,
                    exc,
                )

        product_env = self.env["product.product"].with_context(active_test=not include_inactive)
        if getattr(self, "lang_id", False):
            product_env = product_env.with_context(lang=self.lang_id.code)

        products = product_env.search(domain, offset=offset, limit=limit, order="id")

        currency = getattr(pricelist, "currency_id", False)

        payload = {
            "vendor": {"company_id": self.env.company.id, "company_name": self.env.company.name},
            "customer": {"partner_id": partner.id, "name": partner.display_name},
            "pricelist": {
                "id": pricelist.id,
                "name": pricelist.display_name,
                "currency": {
                    "id": currency.id if currency else False,
                    "name": currency.name if currency else False,
                    "symbol": currency.symbol if currency else False,
                },
            },
            "meta": {
                "limit": limit,
                "offset": offset,
                "count": len(products),
                "include_inactive": include_inactive,
                "only_purchasable": only_purchasable,
            },
            "items": [],
        }

        for product in products:
            payload["items"].append(
                self._serialize_pricelist_item(product=product, partner=partner, pricelist=pricelist)
            )

        _logger.info("Customer pricelist export: returning %s items", len(payload["items"]))
        # OCA endpoint controller json.dumps() must receive JSON-serializable types
        return {"payload": payload}

    def _get_catalog_pricelist(self):
        self.ensure_one()
        if self.catalog_pricelist_id:
            return self.catalog_pricelist_id
        return getattr(self.catalog_partner_id, "property_product_pricelist", False)

    @staticmethod
    def _safe_int(value, default=0):
        try:
            if value is None or value == "":
                return default
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _safe_bool(value, default=False):
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "y", "on")

    @staticmethod
    def _json_date(value):
        """Return a JSON-safe string for date/datetime values."""
        if not value:
            return False
        try:
            if isinstance(value, datetime):
                return fields.Datetime.to_string(value)
            if isinstance(value, date):
                return fields.Date.to_string(value)
        except Exception:
            pass
        return str(value)

    def _serialize_pricelist_item(self, product, partner, pricelist):
        uom_po = getattr(product, "uom_po_id", False) or getattr(product, "uom_id", False)

        price_val, rule_id, rule_min_qty = self._compute_pricelist_price(
            pricelist=pricelist, product=product, partner=partner, qty=1.0, uom=uom_po
        )

        rule = self.env["product.pricelist.item"].browse(rule_id) if rule_id else self.env["product.pricelist.item"]

        lead_time = None
        tmpl = getattr(product, "product_tmpl_id", False)
        if tmpl and hasattr(tmpl, "sale_delay"):
            lead_time = tmpl.sale_delay

        currency = getattr(pricelist, "currency_id", False)

        data = {
            "vendor_product_id": product.id,
            "default_code": getattr(product, "default_code", False),
            "barcode": getattr(product, "barcode", False),
            "name": product.display_name,
            "purchase_uom": {
                "id": uom_po.id if uom_po else False,
                "name": uom_po.name if uom_po else False,
            },
            "price": {
                "value": price_val,
                "currency": {
                    "id": currency.id if currency else False,
                    "name": currency.name if currency else False,
                    "symbol": currency.symbol if currency else False,
                },
                "rule_id": rule_id,
                # These MUST be strings/False (not python datetime/date objects)
                "start_date": self._json_date(rule.date_start) if rule_id and rule.exists() else False,
                "end_date": self._json_date(rule.date_end) if rule_id and rule.exists() else False,
            },
        }

        if rule_min_qty:
            data["min_qty"] = rule_min_qty

        if lead_time is not None:
            try:
                data["lead_time_days"] = float(lead_time)
            except Exception:
                data["lead_time_days"] = lead_time

        return data

    def _compute_pricelist_price(self, pricelist, product, partner, qty=1.0, uom=None):
        price = None
        rule_id = False
        rule_min_qty = None

        try:
            if hasattr(pricelist, "_get_product_price"):
                ctx = {"uom": uom.id} if uom else {}
                price = pricelist.with_context(**ctx)._get_product_price(product, qty, partner)
        except Exception as exc:
            _logger.debug("Pricelist _get_product_price failed for product %s: %s", product.id, exc)

        try:
            if hasattr(pricelist, "_get_product_price_rule"):
                ctx = {"uom": uom.id} if uom else {}
                res = pricelist.with_context(**ctx)._get_product_price_rule(product, qty, partner)
                if isinstance(res, (list, tuple)) and len(res) >= 2:
                    if price is None:
                        price = res[0]
                    rule_id = res[1]
                    if rule_id:
                        rule = pricelist.env["product.pricelist.item"].browse(rule_id)
                        if rule.exists() and hasattr(rule, "min_quantity"):
                            rule_min_qty = rule.min_quantity
        except Exception as exc:
            _logger.debug("Pricelist _get_product_price_rule failed for product %s: %s", product.id, exc)

        if price is None:
            price = getattr(product, "list_price", 0.0)
            _logger.debug("Pricelist price fallback for product %s: list_price=%s", product.id, price)

        try:
            price = float(price)
        except Exception:
            price = 0.0

        return price, rule_id, rule_min_qty
