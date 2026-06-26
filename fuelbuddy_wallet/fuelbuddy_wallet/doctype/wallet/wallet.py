# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Wallet DocType controller plus the cross-doctype enforcement / balance
upkeep it drives.

Everything wallet-related lives here so the feature is self-contained in the
doctype package; the only piece that must sit outside is the event
registration, in ``hooks.py`` ``doc_events`` (Frappe has no other way to bind a
handler onto Delivery Note / Payment Entry). Those handlers were ported from
DocType-Event Server Scripts; the Frappe event labels map to controller hooks:

    "Before Save"  -> validate
    "After Save"   -> on_update
    "After Submit" -> on_submit

A "Wallet" is the row whose ``payment_type`` is literally "Wallet"; a customer
has at most one.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import add_to_date, flt, now_datetime, time_diff_in_seconds

# Hours a breach stays open before it auto-closes (once the wallet has recovered).
BREACH_WINDOW_HOURS = 12

# Payment Terms Template whose customers get an auto-created wallet. Must match the
# record name exactly -- the Wallet form's customer filter uses the same value.
CASH_ADVANCE_TERMS = "Cash advance"

# Master switch. The shared "Fuelbuddy Settings" single (hosted in fuelbuddy_crm,
# read by all FuelBuddy apps) gates this whole feature via its "Enable Wallet" flag.
# When off (the default), every cross-doctype hook below no-ops: no wallets are
# auto-created on customer onboarding and Delivery Notes are not blocked on wallet
# balance.
FB_SETTINGS_DOCTYPE = "Fuelbuddy Settings"


def _wallet_enabled():
	"""True when the Wallet feature is enabled in Fuelbuddy Settings (default off)."""
	return bool(frappe.db.get_single_value(FB_SETTINGS_DOCTYPE, "enable_wallet"))


class Wallet(Document):
	def before_save(self):
		# When breach is (re)enabled, stamp date_of_breach = now so the 12h window
		# (close_expired_breaches + the Delivery Note blocker) is measured from the
		# moment it was turned on. Set on the doc itself so it saves in one write and
		# shows on the form immediately (cleaner than a post-save on_update write).
		if self.enable_breach and self.has_value_changed("enable_breach"):
			self.date_of_breach = now_datetime()


# -- balance helpers ---------------------------------------------------------


def get_customer_wallet(customer):
	"""Return the wallet name for ``customer`` (payment_type == "Wallet"), or None."""
	if not customer:
		return None
	return frappe.db.get_value(
		"Wallet", {"customer": customer, "payment_type": "Wallet"}, "name"
	)


def customer_gl_balance(customer):
	"""Net GL balance (credit - debit) over non-cancelled GL Entries for the customer."""
	rows = frappe.db.get_all(
		"GL Entry",
		filters={"is_cancelled": 0, "party_type": "Customer", "party": customer},
		fields=["credit_in_account_currency", "debit_in_account_currency"],
	)
	ledger = 0.0
	for r in rows:
		ledger += (r.credit_in_account_currency or 0) - (r.debit_in_account_currency or 0)
	return ledger


def customer_delivered_total(customer):
	"""Sum of Delivery Note grand_total (draft + submitted) for the customer."""
	rows = frappe.db.get_all(
		"Delivery Note",
		filters={"customer": customer, "docstatus": ["in", [0, 1]]},
		fields=["grand_total"],
	)
	return sum((d.grand_total or 0) for d in rows)


def recompute_from_deliveries(wallet_name, customer):
	"""Refresh received / delivered / remaining from GL and Delivery Notes."""
	ledger = customer_gl_balance(customer)
	delivered = customer_delivered_total(customer)
	frappe.db.set_value(
		"Wallet",
		wallet_name,
		{
			"amount_received": ledger,
			"amount_delivered": delivered,
			"amount_remaining": ledger - delivered,
		},
	)


def recompute_received(wallet_name):
	"""Refresh received (and remaining, against the stored delivered) from GL."""
	customer = frappe.db.get_value("Wallet", wallet_name, "customer")
	ledger = customer_gl_balance(customer)
	delivered = flt(frappe.db.get_value("Wallet", wallet_name, "amount_delivered"))
	frappe.db.set_value(
		"Wallet",
		wallet_name,
		{"amount_received": ledger, "amount_remaining": ledger - delivered},
	)


# -- cross-doctype event handlers (wired in hooks.py doc_events) -------------


def enforce_wallet_balance(doc, method=None):
	"""Delivery Note `validate` (was "Delivery Note Wallet blocker", Before Save).

	Block a Delivery Note that would push the customer's wallet below zero --
	accounting for other open drafts already reserving the balance -- unless an
	active breach allowance covers the shortfall. On a hard block, raise a
	support Issue (in its own transaction so it survives the rollback) and throw.
	"""
	if not _wallet_enabled():
		return  # feature disabled in Fuelbuddy Settings -> never block on wallet balance
	wallet = frappe.db.get_value(
		"Wallet",
		{"customer": doc.customer, "payment_type": "Wallet"},
		["name", "amount_remaining", "enable_breach", "breach_amount", "date_of_breach"],
		as_dict=True,
	)
	if not wallet:
		return

	remaining = flt(wallet.amount_remaining)

	# Reserve OTHER open drafts; current doc is added via dn_amount below (counted once).
	drafts = frappe.get_all(
		"Delivery Note",
		filters={"customer": doc.customer, "docstatus": 0, "name": ["!=", doc.name]},
		fields=["grand_total"],
	)
	draft_total = sum(flt(d.grand_total) for d in drafts)

	available = remaining - draft_total
	dn_amount = flt(doc.grand_total)

	if (available - dn_amount) >= 0:
		return

	shortfall = dn_amount - available

	breach_active = False
	if wallet.enable_breach:
		breach_active = True
		if wallet.date_of_breach:
			hrs = time_diff_in_seconds(now_datetime(), wallet.date_of_breach) / 3600.0
			# close keyed to REAL wallet recovery (remaining), not draft-adjusted available
			if hrs >= BREACH_WINDOW_HOURS and remaining >= 0:
				frappe.db.set_value(
					"Wallet", wallet.name, {"enable_breach": 0, "breach_amount": 0}
				)
				breach_active = False

	allowed = breach_active and (shortfall <= flt(wallet.breach_amount))

	if allowed:
		if not wallet.date_of_breach:
			frappe.db.set_value("Wallet", wallet.name, "date_of_breach", now_datetime())
		return

	subject = "Wallet balance insufficient for Delivery Note " + str(doc.customer)
	# Reuse an already-open ticket if there is one; otherwise raise a fresh Issue in
	# its own transaction (frappe.enqueue) so it survives the throw-rollback below.
	# The full breakdown lives on the Issue, not in the user-facing block dialog.
	ticket = frappe.db.get_value(
		"Issue", {"subject": subject, "status": ["!=", "Closed"]}, "name"
	)
	if not ticket:
		description = (
			"Customer: " + str(doc.customer) + "<br>"
			"DN Amount: " + str(dn_amount) + "<br>"
			"Wallet Remaining: " + str(remaining) + "<br>"
			"Open Drafts Reserved: " + str(draft_total) + "<br>"
			"Available: " + str(available) + "<br>"
			"Shortfall: " + str(shortfall) + "<br>"
			"Breach Enabled: " + str(wallet.enable_breach) + "<br>"
			"Breach Amount: " + str(flt(wallet.breach_amount))
		)
		frappe.enqueue(
			"frappe.client.insert",
			queue="short",
			doc={
				"doctype": "Issue",
				"subject": subject,
				"priority": "High",
				"issue_type": "Error Log",
				"description": description,
			},
		)

	# Stop the punch. Frappe can only halt a save by raising, so this stays a throw --
	# but a short one; the detail is on the Issue, not dumped at the user.
	ticket_note = (
		"Support ticket " + str(ticket) + " is open."
		if ticket
		else "A support ticket has been raised."
	)
	frappe.throw(
		"Wallet limit exceeded for " + str(doc.customer) + ". " + ticket_note,
		title="Wallet Limit Exceeded",
	)


def update_wallet_on_delivery_note(doc, method=None):
	"""Delivery Note `on_update` (was "Wallet update on DN", After Save).

	Refresh received / delivered / remaining on the customer's wallet.
	"""
	if not _wallet_enabled():
		return
	wallet = get_customer_wallet(doc.customer)
	if wallet:
		recompute_from_deliveries(wallet, doc.customer)


def update_wallet_on_payment_entry(doc, method=None):
	"""Payment Entry `on_submit` (was "Wallet amount update", After Submit).

	Refresh received (and remaining, against stored delivered) when a customer
	payment is submitted.
	"""
	if not _wallet_enabled():
		return
	if doc.party_type != "Customer":
		return
	wallet = get_customer_wallet(doc.party)
	if wallet:
		recompute_received(wallet)


def create_wallet_for_customer(doc, method=None):
	"""Customer `after_insert`: auto-create a Wallet when the new customer is on
	Cash-advance terms. Skips if a wallet already exists for the customer.
	"""
	if not _wallet_enabled():
		return
	if doc.payment_terms != CASH_ADVANCE_TERMS:
		return
	if get_customer_wallet(doc.name):
		return
	frappe.get_doc(
		{
			"doctype": "Wallet",
			"customer": doc.name,
			"payment_terms": doc.payment_terms,
			"payment_type": "Wallet",
		}
	).insert(ignore_permissions=True)


# -- scheduled tasks (wired in hooks.py scheduler_events) ---------------------


def close_expired_breaches():
	"""Disable breach and zero out breach_amount for every wallet whose breach has
	been open for at least BREACH_WINDOW_HOURS, measured from date_of_breach.

	Runs on a schedule so a breach expires on its own ~12h after it was enabled,
	independent of any Delivery Note activity. Wallets with breach enabled but no
	date_of_breach are skipped (the <= comparison excludes NULLs); date_of_breach is
	always stamped when breach is enabled (see Wallet.before_save), so that's a no-op
	in practice. Returns the list of closed wallet names.
	"""
	if not _wallet_enabled():
		return []
	cutoff = add_to_date(now_datetime(), hours=-BREACH_WINDOW_HOURS)
	names = frappe.get_all(
		"Wallet",
		filters={"enable_breach": 1, "date_of_breach": ["<=", cutoff]},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value("Wallet", name, {"enable_breach": 0, "breach_amount": 0})
	if names:
		frappe.db.commit()
	return names
