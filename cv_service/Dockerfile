FROM eaglevision_cv_service AS base

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt

COPY . .

RUN python -c "from ultralytics import YOLO; YOLO('yolov8m.pt')"

CMD ["python", "main.py"]
