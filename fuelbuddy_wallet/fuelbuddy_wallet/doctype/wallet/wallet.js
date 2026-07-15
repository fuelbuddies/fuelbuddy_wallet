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
		if (!frm.is_new()) {
			frm.add_custom_button(__("Reconcile from Transactions"), () => {
				frappe.call({
					method: "fuelbuddy_wallet.fuelbuddy_wallet.doctype.wallet.wallet.reconcile_wallet",
					args: { wallet_name: frm.doc.name },
					freeze: true,
					callback: (r) => show_wallet_reconcile(frm, r.message),
				});
			});
		}
	},
});

// Dry-run first: show stored vs live totals; only write when the user confirms.
function show_wallet_reconcile(frm, res) {
	const labels = {
		amount_received: __("Amount Received"),
		amount_delivered: __("Amount Delivered"),
		amount_remaining: __("Amount Remaining"),
	};
	const fmt = (v) => format_currency(v, frappe.defaults.get_default("currency"));
	const rows = Object.keys(labels)
		.map((f) => {
			const bad = Math.abs(res.stored[f] - res.expected[f]) >= 0.005;
			return `<tr${bad ? ' style="color:var(--red-600);font-weight:600;"' : ""}>
				<td>${labels[f]}</td>
				<td style="text-align:right">${fmt(res.stored[f])}</td>
				<td style="text-align:right">${fmt(res.expected[f])}</td>
			</tr>`;
		})
		.join("");
	const table = `<table class="table table-bordered">
		<thead><tr><th></th><th style="text-align:right">${__("Stored")}</th>
		<th style="text-align:right">${__("Live Transactions")}</th></tr></thead>
		<tbody>${rows}</tbody></table>`;

	if (res.in_sync) {
		frappe.msgprint({ title: __("Wallet is in sync"), message: table, indicator: "green" });
		return;
	}
	const d = new frappe.ui.Dialog({
		title: __("Wallet out of sync"),
		primary_action_label: __("Correct Now"),
		primary_action: () => {
			frappe.call({
				method: "fuelbuddy_wallet.fuelbuddy_wallet.doctype.wallet.wallet.reconcile_wallet",
				args: { wallet_name: frm.doc.name, apply: 1 },
				freeze: true,
				callback: () => {
					d.hide();
					frm.reload_doc();
					frappe.show_alert({ message: __("Wallet corrected"), indicator: "green" });
				},
			});
		},
	});
	d.$body.html(table);
	d.show();
}
