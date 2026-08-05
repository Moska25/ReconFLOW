# Sample bank statements

Synthetic demo files, generated from the seeded dataset in this repo. Neither is a
real bank statement and the account number is not a real IBAN.

Upload **one** of them on http://127.0.0.1:8012/import and then re-run matching: each
settles the same eight unpaid GEL invoices, so the auto-match rate moves.

Upload both and you will see the honest consequence rather than a tidied one. The two
files carry different transaction ids, so the content hash cannot tell they are the same
statement, both post, and the same cash lands twice. The second set has nothing left to
match and the duplicate detector raises it. That is what a bank sending you the same
statement in two formats actually does to a reconciliation system.

- `statement_mt940.sta` - SWIFT MT940, tags 20/25/28C/60F/61/86/62F.
- `statement_camt053.xml` - ISO 20022 CAMT.053.

The format is detected from the content, not the file extension.
