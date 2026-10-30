# Deploy guide: Team Spark Stock Tracker

Two free options. **Render is the easier one** (no console work). Start command for any host: `gunicorn app:app`.

> Free hosts may wipe the SQLite file on restart (the hackathon brief says that's fine for a demo). The app has a **Load demo data** button on an empty Products page, so you can refill it in one click before judging.

---

## 0. Run it on your laptop first (2 minutes)

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. If the status dot in the top-left is green and the Products page loads, the backend is fine. Click **Load demo data** to see everything working.

---

## 1. Put the code on GitHub and open the Pull Request

Do this with your team's repo (`team-spark`) and follow whatever branch/PR instructions the organizers gave you for the Voyage repository.

```bash
git clone <your team-spark repo URL>
cd team-spark
git checkout -b feature/stock-tracker
# copy app.py, index.html, requirements.txt, README.md, DEPLOY.md, .gitignore into this folder
git add .
git commit -m "Build stock tracker: products, sales, restock log, low-stock alerts, reports"
git push -u origin feature/stock-tracker
```

Then open the repo on GitHub, click **Compare & pull request**, and create the PR against the repository the organizers assigned to your team. Only one teammate needs to open the PR.

`.gitignore` already keeps `stock.db` and `__pycache__` out of the commit.

---

## 2. Deploy on Render (recommended)

1. Go to https://render.com and sign in with GitHub.
2. **New > Web Service**, then connect the GitHub repo that contains `app.py`.
3. Fill in:
   - **Language:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
   - **Instance type:** Free
4. Click **Create Web Service**. When the build finishes you get a live `https://<name>.onrender.com` link. Every push to the linked branch redeploys automatically.
5. Open the link, click **Load demo data**, and check each tab.

Free instances typically go to sleep when idle, so the first load after a quiet period can take a while. **Open the link a couple of minutes before you demo or submit.**

Optional settings (Environment tab on Render):

| Variable | Meaning | Default |
|---|---|---|
| `SHOP_NAME` | Name printed on invoices | `Team Spark Store` |
| `TZ_OFFSET_MINUTES` | Shop clock offset from UTC, used for "today" in reports | `330` (India) |
| `DB_PATH` | Where the SQLite file lives | `stock.db` next to `app.py` |

---

## 3. Alternative: PythonAnywhere

1. Create a free account at https://www.pythonanywhere.com.
2. Open a **Bash console** and run:
   ```bash
   git clone <your repo URL> mysite
   mkvirtualenv --python=/usr/bin/python3.13 spark-env
   pip install -r mysite/requirements.txt
   ```
   (Use the Python version PythonAnywhere offers; it only needs to match between the virtualenv and the web app.)
3. **Web** tab > **Add a new web app** > **Manual configuration** > pick the same Python version.
4. In the **Virtualenv** box enter `spark-env`.
5. Click the **WSGI configuration file** link and replace its contents with:
   ```python
   import sys
   path = '/home/YOURUSERNAME/mysite'
   if path not in sys.path:
       sys.path.insert(0, path)

   from app import app as application
   ```
6. Back on the Web tab press **Reload**, then open `YOURUSERNAME.pythonanywhere.com`.

(Official guide: https://help.pythonanywhere.com/pages/Flask/)

---

## Submission checklist

- [ ] Live link opens and the status dot is green
- [ ] Add a product, sell 2+ items in one sale, try selling more than the stock (it must be rejected)
- [ ] Restock a product and check its **History**
- [ ] Low-stock badge shows, Report tab shows revenue / profit / top 5
- [ ] PR opened on the assigned Voyage repo
- [ ] Submitted on the MLH platform (OrganizerHQ) with the live link and PR link

## Troubleshooting

- **Build fails on Render:** check the Build Command and Start Command exactly as above, and that `requirements.txt` is in the repo root next to `app.py`.
- **"Application error" / 502:** open the service's Logs. A typo in the start command (`app:app` = file `app.py`, variable `app`) is the usual cause.
- **Camera scan does nothing:** it only works on HTTPS in Chrome (the Render link is HTTPS). Typing or USB-scanning a SKU into the Sell search box always works.
- **Data vanished:** the free host restarted. Click **Load demo data** (only shown when the shop is empty).
