# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WalletBlock(Document):
	# All create/release logic lives in wallet.py (block_wallet_amount /
	# release_wallet_block) under the Wallet row lock; rows are plain records.
	pass
