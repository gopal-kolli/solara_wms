from unittest import TestCase


class TestShopifyAddressSync(TestCase):
    def test_normalises_shopify_shipping_address(self):
        from solara_wms.wms import shopify_address_values as sync

        result = sync.address_values({
            "name": "Asha Kumar",
            "address1": "12 Lake Road",
            "address2": "Near Metro",
            "city": "Bengaluru",
            "province": "Karnataka",
            "zip": "560 001",
            "country": "India",
            "phone": "+91 98765 43210",
        })

        self.assertEqual(result["address_line1"], "12 Lake Road")
        self.assertEqual(result["pincode"], "560001")
        self.assertEqual(result["state"], "Karnataka")
        self.assertEqual(result["phone"], "+91 98765 43210")

    def test_detects_a_material_address_change(self):
        from solara_wms.wms import shopify_address_values as sync

        atlas = {
            "address_line1": "12 Lake Road", "address_line2": "",
            "city": "Bengaluru", "state": "Karnataka", "pincode": "560001",
            "country": "India", "phone": "9876543210",
        }
        shopify = {
            "address_line1": "12 Lake Road", "address_line2": "",
            "city": "Bengaluru", "state": "Karnataka", "pincode": "560035",
            "country": "India", "phone": "9876543210",
        }

        self.assertFalse(sync.addresses_match(atlas, shopify))

    def test_equivalent_phone_and_case_do_not_create_a_change(self):
        from solara_wms.wms import shopify_address_values as sync

        atlas = {
            "address_line1": "12 lake road", "address_line2": "",
            "city": "Bengaluru", "state": "Karnataka", "pincode": "560001",
            "country": "India", "phone": "+91 98765 43210",
        }
        shopify = {
            "address_line1": "12 Lake Road", "address_line2": "",
            "city": "BENGALURU", "state": "Karnataka", "pincode": "560001",
            "country": "india", "phone": "9876543210",
        }

        self.assertTrue(sync.addresses_match(atlas, shopify))

    def test_state_change_is_identified_for_gst_review(self):
        from solara_wms.wms import shopify_address_values as sync

        atlas = {"state": "Telangana"}
        shopify = {"state": "Karnataka"}

        self.assertTrue(sync.state_changed(atlas, shopify))

    def test_slack_digest_contains_order_ids_but_not_addresses(self):
        from solara_wms.wms import shopify_address_values as sync

        text = sync.render_address_exception_slack([
            {"shopify_order_number": "SOL1249001", "delivery_note": "SHPDN27-1",
             "reason": "Address changed after Delivery Note creation"}
        ], "08 Aug 09:00")

        self.assertIn("SOL1249001", text)
        self.assertIn("SHPDN27-1", text)
        self.assertNotIn("address_line1", text)
