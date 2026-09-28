# WEAVE — Build Your Personal Knowledge Base

WEAVE is a method for collecting everything you learn — from courses, books, articles, videos, and your own notes — into a single structured Notion workspace that you can then query with Claude in plain English.

The idea is simple: as you learn things, you **weave** them into a personal library. When you need to recall, connect, or build on what you've learned, you ask Claude instead of hunting through folders.

---

## What you'll have when you're done

- A Notion database that holds all your course notes, resources, and references
- A local app that keeps Claude up to date with your knowledge base
- The ability to ask Claude questions like:
  - *"What did I learn about neural networks?"*
  - *"Compare my notes on React and Vue"*
  - *"What resources do I have on negotiation?"*
  - *"Summarise my Python course notes in three bullet points"*

---

## Before you start

You need:
- A free [Notion account](https://www.notion.so)
- This app installed and running (see [README.md](README.md) for setup)
- About 30 minutes for the first-time setup

---

## Part 1 — Set up your Notion workspace

### Step 1: Create a free Notion account

Go to [notion.so](https://www.notion.so) and sign up. The free plan is more than enough to get started — it gives you unlimited pages and databases.

### Step 2: Create your Reference Library database

This is the core of your knowledge base. Every course, book, article, video, and note you collect will live here as a row in this database.

1. In Notion, click **+ New page** in the left sidebar
2. Give the page a title like **Knowledge Library** or **My Learning Hub**
3. On the new page, type `/database` and choose **Database — Full page**
4. Name the database **Reference Library**

### Step 3: Add the right properties (columns)

Your database needs columns to organise what you store. Here's a recommended set — click the `+` button at the top of any column to add new ones:

| Property name | Type | What it's for |
|---------------|------|---------------|
| **Name** | Title | The title of the resource (already exists by default) |
| **Type** | Select | Course / Book / Article / Video / Note / Tool |
| **Source** | URL | Link to the original (website, course platform, file path) |
| **Status** | Select | Not started / In progress / Complete / On hold |
| **Topics** | Multi-select | Subject tags: Python, Marketing, Design, etc. |
| **Rating** | Select | ⭐ / ⭐⭐ / ⭐⭐⭐ — how useful was it? |
| **Notes** | Text | Your own summary or key takeaways |
| **Date added** | Date | When you added this resource |
| **File** | Files & media | For attaching PDFs, slides, or images |

> **Tip:** You don't need all of these on day one. Start with Name, Type, Topics, and Notes — add more as you figure out what's useful.

### Step 4: Connect your integration

For Claude to read your database, you need to share it with the Notion integration you created during app setup.

1. Open your **Reference Library** database in Notion
2. Click the `…` menu (top-right) → **Connections**
3. Search for the integration name you created (e.g. "Local Sync") → click **Confirm**

The database is now shared with the app and will be picked up on the next sync.

---

## Part 2 — Structuring what you know

### How to add a course

When you complete or start a course, add it to your Reference Library:

1. Click **+ New** to add a row
2. **Name**: The course title (e.g. "Python for Beginners — Codecademy")
3. **Type**: Course
4. **Source**: The URL of the course
5. **Status**: In progress (or Complete if you've finished)
6. **Topics**: Add relevant tags (Python, Programming, etc.)
7. **Notes**: Paste in your key takeaways, or write a few sentences about what you learned

You can also create **sub-pages** inside a row for more detailed notes — click on the row, then click **Open full page**, and type your notes directly in the Notion page body.

### How to add a book

Same process:
1. Name: Book title + Author
2. Type: Book
3. Topics: Subject tags
4. Notes: A short summary of the main ideas, quotes you liked, or what you'd apply

### How to add your own notes or ideas

You don't need an external source — you can store your own thinking too:
1. Type: Note
2. Name: The concept or idea (e.g. "How I learn new frameworks")
3. Notes: Write freely — this is your space

---

## Part 3 — Importing resources from your PC

### PDFs and documents

If you have course slides, ebooks, or other PDFs saved on your computer:

**Option A — Attach directly to Notion:**
1. Open the Notion row for the resource
2. Click the **File** property → **Upload a file**
3. Select your PDF — it uploads to Notion's storage

**Option B — Link by local path:**
In the **Source** or **Notes** field, note the file path on your PC (e.g. `C:\Courses\Python\slides.pdf`). The app downloads and mirrors anything Notion stores; local-only paths are just for your reference.

### Videos and online courses

For video courses (Udemy, YouTube, etc.):
1. Add the course as a row with **Type: Course**
2. Paste the video/course URL in the **Source** field
3. Write your notes in the **Notes** field or in the page body
4. If you downloaded slides or a transcript, attach those as files

### Handwritten notes or images

If you take handwritten notes and scan or photograph them:
1. Upload the image to the Notion row's **File** property or drag it into the page body
2. Add a text summary in **Notes** so Claude can search for the content (Claude reads the text, not images)

---

## Part 4 — Making Claude aware of your knowledge base

Once the app is running and synced:

1. Open Claude Desktop
2. In a new conversation, start with: *"Load my Notion knowledge base"* or just ask a question directly
3. Claude will check the local index and answer from your library

### Useful things to ask Claude

**Discovery:**
- *"What topics do I have resources on?"*
- *"Show me everything I've added in the last month"*
- *"What courses do I have marked as complete?"*

**Learning:**
- *"Summarise my notes on machine learning"*
- *"What are the key ideas from my notes on negotiation?"*
- *"Explain the concept of recursion based on my Python course notes"*

**Connections:**
- *"Are there any links between my marketing notes and my psychology notes?"*
- *"Which resources mention the Pareto principle?"*
- *"What do I know about building APIs? Combine all my relevant notes"*

**Planning:**
- *"Based on my notes, what topics am I weakest on?"*
- *"What should I study next to build on what I already know about web development?"*

---

## Part 5 — Keeping your knowledge base healthy

### Good habits

- **Add as you go.** It takes 2 minutes to add a resource. Batching it once a month takes an hour and you'll miss things.
- **Write the Notes field first.** Even a single sentence — "This explains why X works" — makes it 10× more searchable than having no summary.
- **Use consistent topic tags.** "Python", not "python", "Python3", and "py" — pick one and stick to it. Claude searches by keyword so consistency matters.
- **Rate resources after you use them.** A ⭐⭐⭐ rating tells you (and Claude) that something was genuinely useful.

### Keeping it synced

The app syncs automatically every few minutes. You can also:
- Right-click the tray icon → **Force Sync Now** to pull immediately
- Check the tray icon colour: green = synced, amber = syncing, red = error

### Backing up your knowledge base

The app can export a backup to any folder (including Google Drive) every night:
1. Right-click tray icon → **Settings** → **Backup**
2. Choose a folder (a Google Drive folder works perfectly)
3. Set how many days of backups to keep
4. Click **Backup Now** to test it

---

## Recommended database structures for different use cases

### For a developer / programmer

| Property | Values |
|----------|--------|
| Type | Course, Tutorial, Documentation, Tool, Library, Snippet |
| Language | JavaScript, Python, SQL, etc. |
| Difficulty | Beginner / Intermediate / Advanced |
| Status | Bookmarked / Reading / Done |

### For a student or researcher

| Property | Values |
|----------|--------|
| Type | Lecture, Textbook, Paper, Article, Video, Podcast |
| Subject | Statistics, Economics, Biology, etc. |
| Module / Course | Link to the parent course |
| Key concepts | Multi-select tags for important ideas |

### For a professional / business person

| Property | Values |
|----------|--------|
| Type | Book, Article, Course, Template, Process, Meeting note |
| Category | Sales, Marketing, Operations, Strategy, Finance |
| Application | Immediate / Future / Reference only |
| Client / Project | Which client or project it's relevant to |

---

## Frequently asked questions

**Do I need to pay for Notion?**
No. The free Notion plan supports unlimited databases and pages, which is more than enough for a personal knowledge base.

**How much does it cost to run?**
The app itself is free and open source. PostgreSQL is free. Claude Desktop has a free tier. The optional self-healing agent uses the Anthropic API, which has pay-as-you-go pricing — but it only runs when there's a sync error, so usage is minimal.

**Can I have more than one database?**
Yes. In Settings, set **Database IDs** to `ALL` to sync every database you've shared with your integration. Or list specific IDs separated by commas. You can have as many databases as you like — one for books, one for courses, one for project notes.

**What if I already have a Notion workspace set up?**
Just share your existing databases with the integration (Settings → Connections in each database) and set Database IDs to `ALL`. The app will pick up everything you've shared.

**Is my data private?**
Yes. The app stores a local copy of your Notion data in PostgreSQL on your own machine. Nothing is sent to any server except the Notion API (to fetch your pages) and the Anthropic API (only if the self-healing feature is enabled and a sync error occurs). Your actual content never leaves your machine.

**What if I don't have courses on my PC — can I still use this?**
Absolutely. The app works with any Notion content — it doesn't matter whether your resources are stored locally or just described in Notion. You can write notes directly in Notion and they'll be searchable through Claude.
