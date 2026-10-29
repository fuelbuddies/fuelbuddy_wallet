# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

# Written only by wallet.py (flag from_wallet) at docstatus 1, like GL Entry; edits go through the ORM so Track Changes keeps old values.

import frappe
from frappe.model.document import Document


class WalletLedgerEntry(Document):
	def validate(self):
		self._only_from_wallet()

	def before_update_after_submit(self):
		self._only_from_wallet()

	def before_cancel(self):
		frappe.throw("A Wallet Ledger Entry is never cancelled; the wallet ticks Is Cancelled on it.")

	def on_trash(self):
		frappe.throw("A Wallet Ledger Entry cannot be deleted.")

	def _only_from_wallet(self):
		if not self.flags.from_wallet:
			frappe.throw("Wallet Ledger Entries are written by the wallet only.")


def on_doctype_update():
	frappe.db.add_index("Wallet Ledger Entry", ["voucher_type", "voucher_no"])
	frappe.db.add_index("Wallet Ledger Entry", ["customer", "posting_date"])
