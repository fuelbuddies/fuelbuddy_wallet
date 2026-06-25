// Copyright (c) 2026, Fuelbuddy and contributors
// For license information, please see license.txt

// Ported from the "Wallet Filter" Client Script (code-first, no DB Client Scripts):
// only customers on the "Cash advance" payment terms can back a wallet.
frappe.ui.form.on("Wallet", {
	refresh(frm) {
		frm.set_query("customer", () => ({
			filters: {
				payment_terms: "Cash advance",
			},
		}));
	},
});
