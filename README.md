# Brand Discovery Engine

This is a starter Python tool for your naming due diligence.

## What it does automatically

- Reads candidate names from `candidates.csv`
- Creates clean domain slugs
- Checks whether domains have DNS records
- Searches Apple Podcasts through the public iTunes Search API
- Optionally searches YouTube using the YouTube Data API
- Optionally searches Google using SerpAPI
- Generates one-click links for:
  - Google
  - GoDaddy
  - Namecheap
  - Porkbun
  - USPTO
  - YouTube
  - Instagram
  - Facebook
  - TikTok
  - LinkedIn
  - X
  - Apple Podcasts
  - Spotify
- Scores the names and creates an Excel workbook

## What it does NOT do

It does not directly scrape Instagram, Facebook, TikTok, LinkedIn, or X. Those platforms are best checked manually from the generated links.

It also does not provide legal trademark clearance. It creates USPTO links and an initial risk workflow.

## Install

Open Command Prompt or PowerShell in this folder:

```bash
pip install -r requirements.txt
```

## Run

```bash
python brand_engine.py --input candidates.csv --output brand_results.xlsx
```

## Optional API keys

For YouTube search:

```bash
set YOUTUBE_API_KEY=your_key_here
```

For Google search through SerpAPI:

```bash
set SERPAPI_KEY=your_key_here
```

Then run the script again.

## How to use the output workbook

Open `brand_results.xlsx`.

Start on the `Dashboard` tab.

Then go to `Brand Research`.

For each finalist:

1. Review the automated fields:
   - `.com DNS exists`
   - Apple Podcast results
   - YouTube results if API key was used
   - Google exact results if SerpAPI key was used

2. Click the generated research links.

3. Fill in:
   - Manual domain status
   - Manual social status
   - Manual trademark risk
   - Notes

4. Sort by `initial_score`.

## Important note on domain checks

A DNS check is not the same thing as domain availability.

- DNS exists = likely registered or active
- DNS does not exist = may be available, but could still be registered without DNS

Always confirm the domain at a registrar before making a final decision.
