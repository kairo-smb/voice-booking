FROM public.ecr.aws/lambda/python:3.12

# Install dependencies
COPY booking_engine/requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Copy application code
COPY booking_engine/ ${LAMBDA_TASK_ROOT}/booking_engine/
COPY lambda_handler.py ${LAMBDA_TASK_ROOT}/

CMD ["lambda_handler.handler"]
