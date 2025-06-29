# bot/scheduler_manager.py
import json
import logging
import uuid
from typing import Optional, Dict

from aiogram import Bot
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.client_tasks.scheduled_tasks import (
    run_scheduled_attack, run_scheduled_spam
)
from bot.database.db_manager import db_manager

logger = logging.getLogger(__name__)

class SchedulerManager:
    def __init__(self, bot: Bot):
        self.scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        self.bot = bot
        self.task_runners = {
            'spam': run_scheduled_spam,
            'attack': run_scheduled_attack
        }

    async def start(self):
        """Loads jobs from DB and starts the scheduler."""
        logger.info("Starting SchedulerManager...")
        try:
            jobs = await db_manager.get_active_scheduled_tasks()
            for job_data in jobs:
                self._add_job_to_scheduler(job_data)
            self.scheduler.start()
            logger.info(f"Scheduler started with {len(self.scheduler.get_jobs())} jobs.")
        except Exception as e:
            logger.critical(f"Failed to start scheduler: {e}", exc_info=True)

    def _add_job_to_scheduler(self, job_data: dict):
        """A helper to add a single job to the APScheduler instance."""
        job_id = job_data['job_id']
        task_type = job_data['task_type']
        runner = self.task_runners.get(task_type)
        if not runner:
            logger.error(f"No runner found for task type '{task_type}' (job_id: {job_id}). Skipping.")
            return

        try:
            self.scheduler.add_job(
                runner,
                trigger=CronTrigger.from_crontab(job_data['cron']),
                id=job_id,
                name=f"{task_type}_{job_data['user_id']}",
                args=[self.bot, job_data['user_id'], job_id, job_data['task_params']]
            )
            logger.info(f"Loaded scheduled job {job_id} ({task_type}) for user {job_data['user_id']}.")
        except Exception as e:
            logger.error(f"Failed to add job {job_id} to scheduler: {e}", exc_info=True)

    async def shutdown(self):
        """Shuts down the scheduler."""
        if self.scheduler.running:
            logger.info("Shutting down scheduler...")
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler shut down.")

    def get_job_details(self, job_id: str) -> Optional[Dict]:
        """Gets details for a specific job, including next run time."""
        job = self.scheduler.get_job(job_id)
        if not job:
            return None
        return {
            "next_run_time": job.next_run_time
        }

    async def add_task(self, user_id: int, task_type: str, cron_expression: str, task_params: Optional[dict] = None) -> Optional[str]:
        """Adds a task to the DB and the running scheduler."""
        job_id = str(uuid.uuid4())
        task_params_json = json.dumps(task_params) if task_params else "{}"
        
        try:
            # Add to DB first
            await db_manager.add_scheduled_task(job_id, user_id, task_type, cron_expression, task_params_json)
            
            # Then add to scheduler
            job_data = {"job_id": job_id, "user_id": user_id, "task_type": task_type, "cron": cron_expression, "task_params": task_params_json}
            self._add_job_to_scheduler(job_data)
            logger.info(f"Successfully scheduled new task {job_id} for user {user_id}.")
            return job_id
        except Exception as e:
            logger.error(f"Failed to schedule new task for user {user_id}: {e}", exc_info=True)
            await db_manager.remove_scheduled_task(job_id)
            return None

    async def remove_task(self, job_id: str):
        """Removes a task from the scheduler and the DB."""
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"Job {job_id} removed from scheduler.")
        except JobLookupError:
            logger.warning(f"Job {job_id} not found in scheduler, but proceeding to remove from DB.")
        
        await db_manager.remove_scheduled_task(job_id)

# Global instance
scheduler_manager: Optional[SchedulerManager] = None # This will be initialized in main.py

def init_scheduler(bot: Bot) -> SchedulerManager:
    """Initializes the global scheduler manager instance."""
    global scheduler_manager
    if scheduler_manager is None:
        scheduler_manager = SchedulerManager(bot)
        logger.info("SchedulerManager instance created and assigned globally.")
    return scheduler_manager