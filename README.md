# PakkaGst

A GST invoicing tool you run from your terminal. No subscription, no account, no cloud.

I'm a freelance dev in India. Every time I had to invoice a client I ended up either hand-fixing a Word template (and occasionally getting the CGST/SGST split wrong, which is a great way to annoy a client's accounts team) or staring at Vyapar/myBillBook's pricing page and closing the tab. I build everything in Termux on my phone anyway, so I just made the tool I wanted.

It asks you questions, you answer them, you get a proper tax invoice as a PDF with a UPI QR code on it. That's the whole pitch.

## What it does

- Correct GST math — CGST+SGST when you and the client are in the same state, IGST when you're not, zero tax if you're not GST-registered
- Bill of Supply generated automatically instead of a Tax Invoice when a GST-registered seller charges zero tax (that's the actual legal rule, not a guess)
- Credit notes, referencing the original invoice, so corrections and refunds aren't just a second invoice with a weird negative number nobody can trace
- Delivery challans, if you ever need to move goods without it being a sale
- HSN/SAC code lookup from a local, searchable table (not just "remember what you typed last time")
- GST return summary export (the GSTR-1 / outward-supply side of GSTR-3B) as CSV, straight from your invoices and credit notes — see the honest caveat on this below
- UPI payment QR code generated on your device, no third-party API, no Pillow headache
- A shareable payment link too, with a WhatsApp message pre-filled for the client
- Quotations, not just invoices
- Save any invoice as a template, re-generate it next month in a couple of taps — and optionally make it recur weekly/monthly/yearly (more on what "automatic" actually means below)
- Amount in words on the PDF, Indian lakh/crore style for INR, standard scale for other currencies
- Multiple business profiles, if you run more than one thing under this tool, each with its own numbering
- Multi-currency, if you bill outside India
- Payment tracking — unpaid / paid / overdue, straight in the terminal
- Everything stored locally in SQLite. Nothing leaves your device unless you tell it to (the QR shortener and the rate-table update check are the only two things that touch the internet, and both fail safely if you're offline)

## About "recurring" invoices, honestly

A template can recur weekly/monthly/yearly. What that actually means: every time you open PakkaGst, it checks whether anything's due and offers to generate it right then — so even if you only open the tool once a week, nothing gets missed, no cron job required. If you're running this somewhere that's actually always on — a VPS, a laptop that's never off — there's also "Enable Daily Auto-Check (cron)" in the menu, which installs a real crontab entry so it can generate the invoice without you opening anything. On plain Termux with no `crontab` available, that option just tells you so and does nothing destructive — the on-open catch-up above still works regardless. I'm not claiming background magic on a phone that Android will kill the moment it's not in your hand; I'm claiming it catches up reliably, which is the part that actually matters.

## What it deliberately doesn't do

E-invoicing (IRN via the government portal) and e-way bills. I looked into both before writing a line of code. E-invoicing is only legally required above ₹5 crore turnover, and even below that it realistically needs a paid GSP subscription to access — not something a freelancer needs. E-way bills are for moving physical goods, not for a service freelancer. If you're running an invoice volume where either of these actually applies to you, you've outgrown this tool and should be looking at Vyapar or an accountant, not a phone CLI.

## Install

```
pkg install python-pillow   # Termux only, one-time
git clone https://github.com/hunzo1/PakkaGST
cd PakkaGST
pip install -e .
pakkagst
```

First run walks you through setting up your business profile. After that it's just `pakkagst` and you pick what you want to do from the menu.

## How to actually use it

First time you run `pakkagst`, it'll ask for your business/freelancer name, your state (needed to figure out CGST+SGST vs IGST), and your GSTIN if you have one — leave that blank if you're not GST-registered, the tool handles it correctly either way (zero tax, Bill of Supply instead of a Tax Invoice). You can also drop in a UPI ID here if you want a payment QR on your invoices, but it's optional.

After that, every time you open it you land on a menu. The one you'll use most is **New Invoice**:

1. Pick an existing client or add a new one (name, state, GSTIN if they have one)
2. Add line items one at a time — description, optional HSN/SAC lookup, quantity, rate, GST %. Leave the description blank when you're done adding items
3. Set a due date, add notes if you want, and that's it

It spits out a PDF — path shown right there in the terminal — and a UPI QR code if you set one up. On Termux the PDF lands under `~/.pakkagst/invoices/`, and you can open it with whatever PDF viewer you've got (or just `termux-open yourfile.pdf` if you have `termux-api` installed), AirDrop/share it over, or cat it into whatever you use to send stuff to clients.

Everything else in the menu does roughly what it says — **View Invoices / Track Payments** to mark something paid or overdue, **Manage Clients** to edit saved clients, **HSN/SAC Lookup** to search the rate table without creating an invoice, **Recurring Templates** if you want a monthly retainer invoice to regenerate itself, **Business Profiles** if you run more than one thing under this tool. Poke around, nothing in there will corrupt your data — worst case you make a test invoice you don't care about (which, yeah, is also how I tested this).

## A couple of things worth knowing

GST rates on the HSN/SAC table aren't pulled live from the government — there's no free feed for that, nobody has one, paid tools update theirs manually too. This tool checks a version file in this repo instead ("Check for GST Rate Table Updates" in the menu) and I'll keep it current. If a rate's wrong, open an issue, I'd genuinely rather know than have it sit stale.

The GST return export is the outward-supply side only — what GSTR-1 and the outward table of GSTR-3B need, built straight from your invoices and credit notes. It does **not** include input tax credit, because this tool has no idea what you've purchased — it only ever sees what you've billed out. So it's not a complete GSTR-3B net-liability number, and it's not a direct-upload file for the GST portal. Hand it to your CA alongside your purchase records, or re-key it into whatever you already use to file. I'd rather undersell this than have someone trust a number it can't actually back up.

## Why not just use [Free-GST-Billing-Software](https://github.com/IamRamgarhia/Free-GST-Billing-Software)?

It's a solid project. It's a Node app that opens in a browser, though, which is a heavier ask if you're living in a terminal on a phone, and it doesn't let you actually search the HSN/SAC code list — you're stuck re-using whatever you've typed before. This is the terminal-first version of that idea. Different tool for a different workflow, not a straight clone. Feature-for-feature the two are close now (credit notes, bills of supply, GSTR export, multiple profiles, recurring — this has all of it too), so pick based on whether you want a browser tab or a terminal prompt.

## Contributing

It's one file on purpose, for now — easy to read end to end, easy to send a patch against. If it grows past that I'll split it up. PRs welcome, especially for HSN/SAC rate corrections.

## License

MIT. Use it, fork it, sell your own paid version if you want, I don't care.
