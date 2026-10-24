# KindleTracker
I wanted a fully automated way to track the books I read in 2026 through my Kindle app — no manual logging, no sending screenshots. My rule is simple: any book over 90% read counts as read.

The problem is that Amazon offers no public Kindle API, and it blocks automated browsers outright. So I built a small Python script that runs on my own computer, uses my own Amazon session, and pulls my library and Whispersync reading progress through the same endpoints the Kindle Cloud Reader uses. It outputs a clean JSON file with each book's title, author, progress percentage, and last-read date, and flags everything over 90%.

What it does:

Fetches your Kindle library and real reading progress from your own account
Handles Amazon's session and device authentication the way the Cloud Reader does
Reports books finished (or nearly finished) for the year, with no credentials ever leaving your machine
Known limitation: it works for Kindle Store books, but personal documents — PDFs sent to Kindle — aren't visible through the Cloud Reader at all, since Amazon excludes them from it. That's still an open problem.
