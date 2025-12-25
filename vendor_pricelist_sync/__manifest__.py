# -*- coding: utf-8 -*-
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
{
    "name": "Vendor Pricelist Sync (Simple)",
    "summary": "Sync a vendor's customer-specific pricelist snapshot into product.supplierinfo",
    "version": "18.0.1.0.5",
    "category": "Purchases",
    "author": "Odoo Community Association (OCA)",
    "license": "AGPL-3",
    "depends": [
        "purchase",
        "product",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/vendor_pricelist_backend.xml",
        "data/ir_cron.xml",
    ],
    "installable": True,
    "application": False,
}
