# Database Cleanup Strategy

## Overview

The bot implements an automatic database cleanup system to prevent unbounded growth while maintaining data for analysis and user experience.

---

## Cleanup Schedule

### Automatic Cleanup (Every News Cycle)

The cleanup runs automatically during the news monitoring loop:

```
Cycle 1-9:   Normal operation
Cycle 10:    Cleanup triggered
Cycle 11-19: Normal operation
Cycle 20:    Cleanup triggered
...
```

### Cleanup Frequency

With default `NEWS_CHECK_INTERVAL_SECONDS=1800` (30 minutes):

| Cleanup Type | Frequency | Interval |
|---|---|---|
| Sent articles | Every 10 cycles | ~5 hours |
| Processed articles | Every 10 cycles | ~5 hours |
| Feedback records | Every 50 cycles | ~25 hours |

---

## What Gets Cleaned

### 1. Sent Articles (`sent_articles` table)

**Purpose:** Track which articles have been sent to prevent duplicates

**Cleanup Policy:**
- Removes records older than 30 days
- Keeps recent records for duplicate detection
- Triggered every 10 news cycles

**Rationale:**
- Articles older than 30 days are unlikely to be duplicated
- Reduces table size while maintaining duplicate detection
- Configurable via `days_to_keep` parameter

**Example:**
```python
deleted = self.db.cleanup_old_sent_articles(days_to_keep=30)
# Removes all records with timestamp < 30 days ago
```

### 2. Processed Articles (`processed_articles` table)

**Purpose:** Store articles that have been processed and sent

**Cleanup Policy:**
- Keeps maximum 2000 most recent articles
- Deletes oldest articles when limit exceeded
- Triggered every 10 news cycles

**Rationale:**
- Prevents database from growing indefinitely
- Keeps recent articles for user search/browsing
- 2000 articles ≈ 1-2 weeks of content (at 100+ articles/day)

**Example:**
```python
pruned = self.db.cleanup_old_processed_articles(max_rows=2000)
# Keeps 2000 newest, deletes older ones
```

### 3. Feedback Records (`feedback` table)

**Purpose:** Store user feedback for analysis

**Cleanup Policy:**
- Removes records older than 90 days
- Triggered every 50 news cycles (~25 hours)

**Rationale:**
- Feedback older than 90 days is less useful for analysis
- Reduces table size while keeping recent feedback
- Less frequent cleanup (less important data)

**Example:**
```python
deleted = self.db._cleanup_old_feedback(days_to_keep=90)
# Removes all feedback older than 90 days
```

---

## Database Size Estimation

### Without Cleanup (Unbounded Growth)

Assuming 100 articles/day:

| Time | Sent Articles | Processed Articles | Feedback | Total |
|---|---|---|---|---|
| 1 month | 3,000 | 3,000 | 300 | ~6.3 MB |
| 6 months | 18,000 | 18,000 | 1,800 | ~37.8 MB |
| 1 year | 36,500 | 36,500 | 3,650 | ~76.5 MB |

### With Cleanup (Bounded)

| Table | Max Records | Approx Size |
|---|---|---|
| sent_articles | ~900 (30 days) | ~0.5 MB |
| processed_articles | 2,000 | ~2.0 MB |
| feedback | ~2,700 (90 days) | ~0.3 MB |
| **Total** | | **~2.8 MB** |

**Savings:** ~97% reduction in database size

---

## Manual Cleanup

### Via Telegram Admin Command

```
/admin db_cleanup YOUR_ADMIN_TOKEN
```

Response shows:
```
✅ ניקוי מסד נתונים הושלם:
• Hashes: 150
• Articles: 45
```

### Via Python

```python
# Clean sent articles
deleted = bot.db.cleanup_old_sent_articles(days_to_keep=30)
print(f"Deleted {deleted} old sent articles")

# Clean processed articles
pruned = bot.db.cleanup_old_processed_articles(max_rows=2000)
print(f"Pruned {pruned} old processed articles")

# Clean feedback
deleted = bot.db._cleanup_old_feedback(days_to_keep=90)
print(f"Deleted {deleted} old feedback records")
```

---

## Configuration

### Adjust Cleanup Retention

Edit the cleanup calls in `continuous_news_monitor()`:

```python
# Keep sent articles for 60 days instead of 30
deleted = self.db.cleanup_old_sent_articles(days_to_keep=60)

# Keep 5000 processed articles instead of 2000
pruned = self.db.cleanup_old_processed_articles(max_rows=5000)

# Keep feedback for 180 days instead of 90
deleted = self._cleanup_old_feedback(days_to_keep=180)
```

### Adjust Cleanup Frequency

Edit the cycle counts in `continuous_news_monitor()`:

```python
# Cleanup every 5 cycles instead of 10 (more frequent)
if cycle_count % 5 == 0:
    deleted = self.db.cleanup_old_sent_articles(days_to_keep=30)

# Cleanup every 100 cycles instead of 50 (less frequent)
if cycle_count % 100 == 0:
    deleted = self._cleanup_old_feedback(days_to_keep=90)
```

---

## Monitoring Cleanup

### Check Cleanup Logs

```bash
# View cleanup operations
grep "Cleaned up\|Pruned" bot.log

# Example output:
# 2024-01-15 10:30:45 - INFO - Cleaned up 150 old article hashes
# 2024-01-15 10:30:46 - INFO - Pruned 45 old processed articles
# 2024-01-15 11:00:00 - INFO - Cleaned up 120 old feedback records
```

### Monitor Database Size

```bash
# Check database file size
ls -lh bot_data.db

# Check table sizes (SQLite)
sqlite3 bot_data.db "SELECT name, COUNT(*) as count FROM sqlite_master WHERE type='table' GROUP BY name;"
```

### Via Health Endpoint

```bash
curl http://localhost:5001/health | jq '.details.db_sent_count'
```

---

## Cleanup Process Details

### Sent Articles Cleanup

```sql
DELETE FROM sent_articles 
WHERE timestamp < '2024-01-15T10:30:00+00:00'  -- 30 days ago
```

**Index Used:** `idx_sent_timestamp` (fast lookup)

### Processed Articles Cleanup

```sql
DELETE FROM processed_articles
WHERE id IN (
    SELECT id FROM processed_articles
    ORDER BY timestamp ASC
    LIMIT 45  -- Delete oldest 45 to keep 2000
)
```

**Index Used:** `idx_processed_timestamp` (fast sorting)

### Feedback Cleanup

```sql
DELETE FROM feedback
WHERE timestamp < '2024-01-15T10:30:00+00:00'  -- 90 days ago
```

**Index Used:** `idx_feedback_user` (optional, for analysis)

---

## Performance Impact

### Cleanup Duration

Typical cleanup operation:
- Sent articles: ~10ms
- Processed articles: ~50ms
- Feedback: ~20ms
- **Total:** ~80ms (negligible)

### Impact on Bot

- Cleanup runs during news cycle (not blocking user commands)
- Minimal CPU usage
- No noticeable impact on performance

---

## Data Retention Policy

### For Different Use Cases

**High Volume (1000+ articles/day):**
```python
cleanup_old_sent_articles(days_to_keep=7)      # 1 week
cleanup_old_processed_articles(max_rows=1000)  # 1 week
cleanup_old_feedback(days_to_keep=30)          # 1 month
```

**Medium Volume (100-500 articles/day):**
```python
cleanup_old_sent_articles(days_to_keep=30)     # 1 month (default)
cleanup_old_processed_articles(max_rows=2000)  # 2 weeks (default)
cleanup_old_feedback(days_to_keep=90)          # 3 months (default)
```

**Low Volume (<100 articles/day):**
```python
cleanup_old_sent_articles(days_to_keep=90)     # 3 months
cleanup_old_processed_articles(max_rows=5000)  # 1-2 months
cleanup_old_feedback(days_to_keep=180)         # 6 months
```

---

## Backup Strategy

Before cleanup, consider:

1. **Regular Backups**
   ```bash
   # Daily backup
   cp bot_data.db bot_data.db.backup.$(date +%Y%m%d)
   ```

2. **Export Important Data**
   ```bash
   # Export feedback for analysis
   sqlite3 bot_data.db "SELECT * FROM feedback;" > feedback_export.csv
   ```

3. **Archive Old Data**
   ```bash
   # Before cleanup, export articles older than 30 days
   sqlite3 bot_data.db "SELECT * FROM processed_articles WHERE timestamp < datetime('now', '-30 days');" > archive.csv
   ```

---

## Troubleshooting

### Database Growing Too Fast

**Symptoms:** Database size increases rapidly despite cleanup

**Solutions:**
1. Reduce `max_rows` for processed articles
2. Reduce `days_to_keep` for sent articles
3. Increase cleanup frequency (reduce cycle count)
4. Check if cleanup is actually running (check logs)

### Cleanup Not Running

**Symptoms:** Database size keeps growing, no cleanup logs

**Solutions:**
1. Check if news monitor thread is running: `/admin queue_status`
2. Check logs for errors: `grep -i error bot.log`
3. Manually trigger cleanup: `/admin db_cleanup TOKEN`
4. Verify database file is writable: `ls -l bot_data.db`

### Duplicate Articles After Cleanup

**Symptoms:** Same article posted multiple times

**Solutions:**
1. Increase `days_to_keep` for sent articles
2. Reduce news check interval
3. Check if cleanup is too aggressive

---

## Future Enhancements

1. **Configurable Cleanup via Environment Variables**
   ```env
   DB_CLEANUP_SENT_DAYS=30
   DB_CLEANUP_PROCESSED_MAX=2000
   DB_CLEANUP_FEEDBACK_DAYS=90
   ```

2. **Cleanup Statistics**
   - Track cleanup history
   - Monitor cleanup performance
   - Alert on cleanup failures

3. **Selective Cleanup**
   - Clean by source
   - Clean by date range
   - Clean by article quality score

4. **Data Export**
   - Automatic export before cleanup
   - Archive to S3/cloud storage
   - Data retention compliance
