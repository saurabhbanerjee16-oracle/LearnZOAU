# ZOAU Deep Dive: Job Automation & Scheduling — From "Fire and Forget" to Intelligent Pipelines

**Previously:** We covered what ZOAU does. Today: **How to actually automate mainframe jobs like a pro.**

This is where ZOAU stops being a curiosity and becomes your competitive advantage.

---

## The Problem ZOAU Solves

### Scenario 1: The 3 AM Emergency
Your legacy batch process hangs. You're woken up. You SSH to the mainframe, manually check the job queue, review logs, submit a recovery job, wait 20 minutes, email the team. 2 hours of your life gone.

### Scenario 2: The Weekly Chore
Every Friday, you run 12 jobs in sequence: extract, transform, load, validate. Each step depends on the previous one succeeding. One fails? You restart everything manually.

### Scenario 3: The Scaling Nightmare
You need to run 50 parallel batch jobs across different datasets. Submitting them one by one takes hours. Managing them? Forget it.

**ZOAU answer:** Write it once in Python. Let it run unattended. Get alerts if anything breaks.

---

## Foundation: The Basic Job Submission

Let's start simple.

```python
#!/usr/bin/env python3
"""
Lesson 1: Submit a JCL job and wait for completion
"""

from zoau import MVSCmd, Jobs

# Step 1: Define your JCL
jcl_script = """
//MYJOB JOB (ACCT),'MY JOB',CLASS=A,MSGCLASS=H
//STEP1 EXEC PGM=IEFBR14
//SYSPRINT DD SYSOUT=*
//SYSIN    DD DUMMY
"""

# Step 2: Submit the job
print("Submitting job...")
job = MVSCmd.submit_job(jcl_script)
print(f"Job ID: {job}")

# Step 3: Wait for completion (with timeout)
print("Waiting for job to complete...")
status = Jobs.wait_for_job(job, max_wait_time=300)  # 5 min timeout
print(f"Final status: {status}")

if status == 'CC 0':
    print("✓ Job completed successfully")
else:
    print(f"✗ Job failed with code: {status}")
```

**What's happening:**
1. We define JCL as a Python string
2. Submit it to z/OS (returns a job ID like `JOB00123`)
3. Poll until the job finishes (instead of manual checking)
4. Inspect the return code

**Real use:** Replace manual job submission. That's step one.

---

## Lesson 2: Capture Output & Parse Results

The job ran. Now what? You need the output.

```python
#!/usr/bin/env python3
"""
Lesson 2: Submit job, wait, retrieve output, parse results
"""

from zoau import MVSCmd, Jobs
import json

# Submit job
jcl = """
//EXTRACT JOB (ACCT),'DAILY EXTRACT',CLASS=A
//STEP1 EXEC PGM=COBOLPROG
//INPUT   DD DSN=PROD.DATA.INPUT,DISP=SHR
//OUTPUT  DD DSN=PROD.DATA.OUTPUT,DISP=(NEW,CATLG)
//REPORT  DD SYSOUT=*
"""

job_id = MVSCmd.submit_job(jcl)
print(f"Submitted: {job_id}")

# Wait for completion
status = Jobs.wait_for_job(job_id, max_wait_time=600)

# Retrieve the spool output (the report)
spool_output = Jobs.get_job_output(job_id)
print("\n--- SPOOL OUTPUT ---")
print(spool_output)

# Parse the output to check for errors
if "ERROR" in spool_output:
    print("✗ Job produced errors")
    # Extract the error line
    for line in spool_output.split('\n'):
        if "ERROR" in line:
            print(f"  {line}")
elif status == 'CC 0':
    print("✓ Job succeeded")
    # Extract summary stats from the output
    for line in spool_output.split('\n'):
        if "RECORDS PROCESSED" in line:
            print(f"  {line}")
else:
    print(f"✗ Unexpected status: {status}")
```

**Key concepts:**
- `get_job_output()` pulls the SYSOUT (the report/log)
- You can parse it like any text file
- Check for keywords ("ERROR", "SUCCESS", record counts)
- React based on what you find

**Real use:** Verify that your job didn't just "complete"—it actually *succeeded*.

---

## Lesson 3: Retry Logic & Failure Handling

Jobs fail. Networks glitch. Files lock. What do you do?

```python
#!/usr/bin/env python3
"""
Lesson 3: Robust job submission with retry logic
"""

from zoau import MVSCmd, Jobs
import time

def submit_with_retry(jcl_text, max_retries=3, retry_delay=30):
    """
    Submit a job with automatic retry on failure
    """
    for attempt in range(1, max_retries + 1):
        try:
            print(f"Attempt {attempt}/{max_retries}: Submitting job...")
            job_id = MVSCmd.submit_job(jcl_text)
            print(f"  Job ID: {job_id}")
            
            # Wait for completion
            status = Jobs.wait_for_job(job_id, max_wait_time=600)
            
            # Check if successful
            if status == 'CC 0':
                print(f"  ✓ Job succeeded on attempt {attempt}")
                return job_id, status
            elif status == 'CC 4':
                # CC 4 = warning but might be recoverable
                print(f"  ⚠ Warning code (CC 4) on attempt {attempt}")
                return job_id, status
            else:
                # Hard failure, retry
                print(f"  ✗ Job failed with {status}. Retrying...")
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                else:
                    raise Exception(f"Job failed after {max_retries} attempts")
                    
        except Exception as e:
            print(f"  ✗ Exception: {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)
            else:
                raise

# Usage
jcl = """
//BATCH JOB (ACCT),'BATCH JOB',CLASS=A
//STEP1 EXEC PGM=MYPROGRAM
//INPUT   DD DSN=DATA.INPUT,DISP=SHR
//OUTPUT  DD DSN=DATA.OUTPUT,DISP=(NEW,CATLG)
"""

try:
    job_id, final_status = submit_with_retry(jcl, max_retries=3, retry_delay=30)
    print(f"\nFinal result: Job {job_id} with status {final_status}")
except Exception as e:
    print(f"\nFailed: {e}")
    # Send alert, log incident, etc.
```

**What's new:**
- Loop on failure, wait between retries
- Distinguish between recoverable (CC 4) and fatal errors (CC 8+)
- Give up after N attempts to avoid infinite loops
- Return the result so caller can decide what to do next

**Real use:** Your 3 AM emergency job is now resilient.

---

## Lesson 4: Chaining Jobs (Pipelines)

One job depends on another. Classic mainframe workflow.

```python
#!/usr/bin/env python3
"""
Lesson 4: Multi-step pipeline with dependencies
"""

from zoau import MVSCmd, Jobs
import sys

def run_pipeline():
    """
    Data pipeline: Extract → Transform → Load → Validate
    Each step feeds the next
    """
    
    jobs = {}
    
    # STEP 1: Extract data from production
    print("\n[STEP 1] Extracting source data...")
    extract_jcl = """
//EXTRACT JOB (ACCT),'EXTRACT',CLASS=A
//STEP1 EXEC PGM=EXTRACT
//INPUT   DD DSN=PROD.CUSTOMER.DB,DISP=SHR
//OUTPUT  DD DSN=WORK.EXTRACT.DATA,DISP=(NEW,CATLG)
"""
    
    jobs['extract'] = MVSCmd.submit_job(extract_jcl)
    status = Jobs.wait_for_job(jobs['extract'], max_wait_time=300)
    if status != 'CC 0':
        print(f"✗ Extract failed: {status}")
        return False
    print(f"✓ Extract complete: {jobs['extract']}")
    
    # STEP 2: Transform data
    print("\n[STEP 2] Transforming data...")
    transform_jcl = """
//TRANSFORM JOB (ACCT),'TRANSFORM',CLASS=A
//STEP1 EXEC PGM=TRANSFORM
//INPUT   DD DSN=WORK.EXTRACT.DATA,DISP=SHR
//OUTPUT  DD DSN=WORK.TRANSFORM.DATA,DISP=(NEW,CATLG)
//SCRATCHPAD DD DSN=WORK.TEMP,DISP=(NEW,DELETE)
"""
    
    jobs['transform'] = MVSCmd.submit_job(transform_jcl)
    status = Jobs.wait_for_job(jobs['transform'], max_wait_time=300)
    if status != 'CC 0':
        print(f"✗ Transform failed: {status}")
        return False
    print(f"✓ Transform complete: {jobs['transform']}")
    
    # STEP 3: Load into target
    print("\n[STEP 3] Loading data...")
    load_jcl = """
//LOAD JOB (ACCT),'LOAD',CLASS=A
//STEP1 EXEC PGM=LOAD
//INPUT   DD DSN=WORK.TRANSFORM.DATA,DISP=SHR
//TARGET  DD DSN=PROD.DATA.CLEAN,DISP=(NEW,CATLG)
//REPORT  DD SYSOUT=*
"""
    
    jobs['load'] = MVSCmd.submit_job(load_jcl)
    status = Jobs.wait_for_job(jobs['load'], max_wait_time=600)
    if status != 'CC 0':
        print(f"✗ Load failed: {status}")
        return False
    print(f"✓ Load complete: {jobs['load']}")
    
    # STEP 4: Validate results
    print("\n[STEP 4] Validating...")
    validate_jcl = """
//VALIDATE JOB (ACCT),'VALIDATE',CLASS=A
//STEP1 EXEC PGM=VALIDATE
//INPUT   DD DSN=PROD.DATA.CLEAN,DISP=SHR
//REPORT  DD SYSOUT=*
"""
    
    jobs['validate'] = MVSCmd.submit_job(validate_jcl)
    status = Jobs.wait_for_job(jobs['validate'], max_wait_time=300)
    output = Jobs.get_job_output(jobs['validate'])
    
    if "VALIDATION PASSED" in output:
        print(f"✓ Validation complete: {jobs['validate']}")
        return True
    else:
        print(f"✗ Validation failed")
        print(output)
        return False

# Run it
try:
    success = run_pipeline()
    if success:
        print("\n" + "="*50)
        print("✓ PIPELINE COMPLETE - All steps successful")
        print("="*50)
    else:
        print("\n" + "="*50)
        print("✗ PIPELINE FAILED - See above for details")
        print("="*50)
        sys.exit(1)
except Exception as e:
    print(f"\n✗ Unexpected error: {e}")
    sys.exit(1)
```

**What's happening:**
- Each step waits for the previous to succeed
- If any step fails, the pipeline stops (fail-fast)
- Output is checked for success markers
- One Python script handles the entire workflow

**Real use:** Your Friday 12-job sequence now runs unattended, reliably.

---

## Lesson 5: Parallel Job Execution (For Speed)

Sometimes jobs are independent. Run them together.

```python
#!/usr/bin/env python3
"""
Lesson 5: Parallel job execution for throughput
"""

from zoau import MVSCmd, Jobs
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

def submit_and_monitor(jcl_text, job_name):
    """
    Submit a single job and wait for completion
    Returns (job_name, job_id, status)
    """
    print(f"Submitting {job_name}...")
    job_id = MVSCmd.submit_job(jcl_text)
    
    start_time = time.time()
    status = Jobs.wait_for_job(job_id, max_wait_time=600)
    elapsed = time.time() - start_time
    
    return (job_name, job_id, status, elapsed)

def run_parallel_jobs():
    """
    Submit multiple independent jobs in parallel
    Example: Process 10 customer regions simultaneously
    """
    
    # Define 10 similar jobs, different regions
    jobs_to_run = {}
    for region in ['EAST', 'WEST', 'NORTH', 'SOUTH', 'CENTRAL', 
                   'NE', 'NW', 'SE', 'SW', 'MID']:
        jcl = f"""
//PROC{region} JOB (ACCT),'PROCESS {region}',CLASS=A
//STEP1 EXEC PGM=PROCESS
//INPUT   DD DSN=DATA.{region}.INPUT,DISP=SHR
//OUTPUT  DD DSN=DATA.{region}.OUTPUT,DISP=(NEW,CATLG)
//REPORT  DD SYSOUT=*
"""
        jobs_to_run[region] = jcl
    
    # Submit all jobs in parallel (max 5 concurrent)
    results = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(submit_and_monitor, jcl, region): region
            for region, jcl in jobs_to_run.items()
        }
        
        # Collect results as they complete
        for future in as_completed(futures):
            region = futures[future]
            try:
                job_name, job_id, status, elapsed = future.result()
                results[region] = {
                    'job_id': job_id,
                    'status': status,
                    'elapsed': elapsed
                }
                print(f"✓ {region}: {job_id} completed in {elapsed:.1f}s ({status})")
            except Exception as e:
                print(f"✗ {region}: {e}")
                results[region] = {'error': str(e)}
    
    # Summary
    print("\n" + "="*50)
    print("PARALLEL EXECUTION SUMMARY")
    print("="*50)
    successful = sum(1 for r in results.values() if r.get('status') == 'CC 0')
    total = len(results)
    print(f"Successful: {successful}/{total}")
    
    for region, result in sorted(results.items()):
        if 'error' in result:
            print(f"  ✗ {region}: {result['error']}")
        else:
            print(f"  ✓ {region}: {result['status']} ({result['elapsed']:.1f}s)")
    
    return successful == total

# Run it
if __name__ == "__main__":
    success = run_parallel_jobs()
    exit(0 if success else 1)
```

**Why this matters:**
- Without parallelism: 10 jobs × 5 min each = 50 minutes
- With parallelism: 5 concurrent × 5 min = ~25 minutes (half the time)
- Mainframe loves parallelism; this lets you harness it

**Real use:** Daily data processing that used to take 2 hours now takes 45 minutes.

---

## Lesson 6: Alerting & Notifications

Something breaks. Who knows? Not you (until your boss calls).

```python
#!/usr/bin/env python3
"""
Lesson 6: Alerting on job failure
"""

from zoau import MVSCmd, Jobs
import smtplib
from email.mime.text import MIMEText
import requests

def send_slack_alert(message, channel='#alerts', severity='error'):
    """
    Send alert to Slack
    Assumes SLACK_WEBHOOK_URL is set in environment
    """
    import os
    
    webhook_url = os.environ.get('SLACK_WEBHOOK_URL')
    if not webhook_url:
        print("Warning: SLACK_WEBHOOK_URL not set")
        return
    
    color = 'danger' if severity == 'error' else 'warning'
    payload = {
        'attachments': [{
            'color': color,
            'title': 'Mainframe Job Alert',
            'text': message,
            'footer': 'ZOAU Alert System'
        }]
    }
    
    requests.post(webhook_url, json=payload)

def send_email_alert(subject, body, recipients):
    """
    Send alert email
    """
    import os
    
    smtp_server = os.environ.get('SMTP_SERVER', 'localhost')
    
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = 'mainframe-alerts@company.com'
    msg['To'] = ', '.join(recipients)
    
    with smtplib.SMTP(smtp_server) as server:
        server.sendmail('mainframe-alerts@company.com', recipients, msg.as_string())

def run_job_with_alerts(jcl, job_name):
    """
    Run a job with full alerting
    """
    try:
        print(f"Starting: {job_name}")
        job_id = MVSCmd.submit_job(jcl)
        print(f"Job ID: {job_id}")
        
        # Wait with timeout
        status = Jobs.wait_for_job(job_id, max_wait_time=600)
        
        if status == 'CC 0':
            print(f"✓ Success: {job_name}")
            # Optional: send completion notification
            send_slack_alert(
                f"✓ {job_name} ({job_id}) completed successfully",
                severity='info'
            )
            return True
        else:
            # Get details for alert
            output = Jobs.get_job_output(job_id)
            error_lines = [l for l in output.split('\n') if 'ERROR' in l or 'ABEND' in l]
            
            # Send alerts
            message = f"""
Job: {job_name}
Job ID: {job_id}
Status: {status}
Errors:
{chr(10).join(error_lines[:5])}
            """
            
            send_slack_alert(message, severity='error')
            send_email_alert(
                f"ALERT: {job_name} failed",
                message,
                recipients=['oncall@company.com', 'mainframe-team@company.com']
            )
            
            print(f"✗ Failed: {job_name}")
            return False
            
    except Exception as e:
        # Send critical alert
        message = f"CRITICAL: {job_name} exception: {str(e)}"
        send_slack_alert(message, severity='error')
        send_email_alert(
            f"CRITICAL: {job_name} failed",
            message,
            recipients=['oncall@company.com']
        )
        raise

# Usage
jcl = """
//DAILY JOB (ACCT),'DAILY BATCH',CLASS=A
//STEP1 EXEC PGM=DAILY
//INPUT   DD DSN=DATA.INPUT,DISP=SHR
//OUTPUT  DD DSN=DATA.OUTPUT,DISP=(NEW,CATLG)
"""

success = run_job_with_alerts(jcl, 'DAILY_BATCH_JOB')
```

**What's included:**
- Slack notifications for failures
- Email alerts to on-call team
- Error extraction from spool output
- Exception handling with critical alerts

**Real use:** You sleep. Your automation stays awake.

---

## Bringing It All Together: A Production-Ready Script

```python
#!/usr/bin/env python3
"""
PRODUCTION EXAMPLE: Daily ETL pipeline with monitoring, retry, and alerts
"""

from zoau import MVSCmd, Jobs
import os
import sys
import json
from datetime import datetime
import requests
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/mainframe-etl.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class ETLPipeline:
    def __init__(self):
        self.start_time = datetime.now()
        self.results = {}
        self.slack_webhook = os.environ.get('SLACK_WEBHOOK_URL')
    
    def log_step(self, step_name, status, details=""):
        msg = f"{step_name}: {status}"
        if details:
            msg += f" ({details})"
        logger.info(msg)
        self.results[step_name] = {
            'status': status,
            'timestamp': datetime.now().isoformat(),
            'details': details
        }
    
    def send_alert(self, title, message, severity='info'):
        if not self.slack_webhook:
            return
        
        color_map = {'error': 'danger', 'warning': 'warning', 'info': 'good'}
        payload = {
            'attachments': [{
                'color': color_map.get(severity, 'good'),
                'title': title,
                'text': message,
                'footer': 'ETL Pipeline'
            }]
        }
        requests.post(self.slack_webhook, json=payload)
    
    def run(self):
        logger.info("="*60)
        logger.info("ETL PIPELINE STARTED")
        logger.info("="*60)
        
        try:
            # Extract phase
            if not self._extract():
                self.send_alert(
                    "ETL Failed",
                    "Extract phase failed",
                    severity='error'
                )
                return False
            
            # Transform phase
            if not self._transform():
                self.send_alert(
                    "ETL Failed",
                    "Transform phase failed",
                    severity='error'
                )
                return False
            
            # Load phase
            if not self._load():
                self.send_alert(
                    "ETL Failed",
                    "Load phase failed",
                    severity='error'
                )
                return False
            
            # Success
            elapsed = (datetime.now() - self.start_time).total_seconds()
            logger.info("="*60)
            logger.info(f"✓ ETL PIPELINE COMPLETE ({elapsed:.0f}s)")
            logger.info("="*60)
            
            self.send_alert(
                "ETL Success",
                f"Pipeline completed in {elapsed:.0f}s",
                severity='info'
            )
            return True
            
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            self.send_alert(
                "ETL Critical Error",
                str(e),
                severity='error'
            )
            return False
    
    def _extract(self):
        logger.info("Extract phase starting...")
        jcl = """
//EXTRACT JOB (ACCT),'EXTRACT',CLASS=A
//STEP1 EXEC PGM=EXTRACT
//INPUT   DD DSN=PROD.SOURCE,DISP=SHR
//OUTPUT  DD DSN=WORK.EXTRACT,DISP=(NEW,CATLG)
"""
        
        try:
            job_id = MVSCmd.submit_job(jcl)
            status = Jobs.wait_for_job(job_id, max_wait_time=600)
            
            if status == 'CC 0':
                self.log_step('Extract', 'SUCCESS', job_id)
                return True
            else:
                self.log_step('Extract', 'FAILED', f"Status: {status}")
                return False
        except Exception as e:
            self.log_step('Extract', 'EXCEPTION', str(e))
            return False
    
    def _transform(self):
        logger.info("Transform phase starting...")
        jcl = """
//TRANSFORM JOB (ACCT),'TRANSFORM',CLASS=A
//STEP1 EXEC PGM=TRANSFORM
//INPUT   DD DSN=WORK.EXTRACT,DISP=SHR
//OUTPUT  DD DSN=WORK.TRANSFORM,DISP=(NEW,CATLG)
"""
        
        try:
            job_id = MVSCmd.submit_job(jcl)
            status = Jobs.wait_for_job(job_id, max_wait_time=600)
            
            if status == 'CC 0':
                self.log_step('Transform', 'SUCCESS', job_id)
                return True
            else:
                self.log_step('Transform', 'FAILED', f"Status: {status}")
                return False
        except Exception as e:
            self.log_step('Transform', 'EXCEPTION', str(e))
            return False
    
    def _load(self):
        logger.info("Load phase starting...")
        jcl = """
//LOAD JOB (ACCT),'LOAD',CLASS=A
//STEP1 EXEC PGM=LOAD
//INPUT   DD DSN=WORK.TRANSFORM,DISP=SHR
//TARGET  DD DSN=PROD.FINAL,DISP=(NEW,CATLG)
"""
        
        try:
            job_id = MVSCmd.submit_job(jcl)
            status = Jobs.wait_for_job(job_id, max_wait_time=600)
            
            if status == 'CC 0':
                self.log_step('Load', 'SUCCESS', job_id)
                return True
            else:
                self.log_step('Load', 'FAILED', f"Status: {status}")
                return False
        except Exception as e:
            self.log_step('Load', 'EXCEPTION', str(e))
            return False

if __name__ == "__main__":
    pipeline = ETLPipeline()
    success = pipeline.run()
    sys.exit(0 if success else 1)
```

---

## Key Takeaways

| Concept | What It Does | When to Use |
|---------|------------|-----------|
| **Wait for Job** | Block until job completes | Every submission |
| **Retry Logic** | Automatically rerun failed jobs | Resilience |
| **Pipelines** | Chain dependent jobs | Multi-step workflows |
| **Parallelism** | Run independent jobs together | Speed/throughput |
| **Alerting** | Notify on failure | Production systems |
| **Logging** | Track every action | Debugging & audit |

---

## Learning Path: Next Steps

1. **Today:** Copy one of these examples and adapt it to your mainframe
2. **Tomorrow:** Add a retry wrapper to your most fragile job
3. **This week:** Build a 2-step pipeline (extract → validate)
4. **This month:** Orchestrate your full daily batch with ZOAU

---

## Real-World Impact

**Before ZOAU:**
- Manual job submission: 30 min/day
- Emergency troubleshooting: 2+ hours/incident
- Job failures detected by: angry users/clients

**After ZOAU:**
- Automated submission: 0 min (it just runs)
- Failures detected automatically: 2 min to alert
- Retry logic means 80% of transient failures self-heal

**Your ROI:** 10-15 hours/month of manual work eliminated. Fewer pages at 3 AM. Better uptime.

---

*Learning note: These patterns scale from a single daily job to 500+ jobs orchestrated across your enterprise. ZOAU is the bridge between legacy mainframe operations and modern DevOps.*

**Coming next:** Data handling—how to grab z/OS data, convert it, and feed it into pandas/Kafka/cloud systems.
