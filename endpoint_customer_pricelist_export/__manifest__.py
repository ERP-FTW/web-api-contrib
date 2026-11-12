# -*- coding: utf-8 -*-
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
{
    "name": "Endpoint Customer Pricelist Export",
    "summary": "Expose a customer-specific pricelist snapshot over OCA endpoint (simple export)",
    "version": "18.0.1.0.1",
    "category": "Tools",
    "author": "Odoo Community Association (OCA)",
    "license": "AGPL-3",
    "depends": [
        "endpoint_product_catalog",
        "endpoint_auth_api_key",
        "endpoint_jsonifier",
        "product",
    ],
    "data": [
        "views/endpoint_endpoint.xml",
    ],
    "installable": True,
    "application": False,
}
