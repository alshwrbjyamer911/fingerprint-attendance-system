/*
 * Fingerprint Attendance System - Arduino Firmware
 * Hardware: Arduino Uno + R307/AS608 Fingerprint Sensor + 16x2 LCD (4-bit)
 *
 * Fingerprint Sensor: SoftwareSerial on pins 2 (RX) and 3 (TX), 57600 baud
 * LCD 16x2 (4-bit): RS=A0, EN=A1, D4=8, D5=9, D6=10, D7=11
 * USB Serial to PC: 9600 baud
 */

#include <Adafruit_Fingerprint.h>
#include <ArduinoJson.h>
#include <LiquidCrystal.h>
#include <SoftwareSerial.h>

// ─── Pin Definitions ───
#define FP_RX 6
#define FP_TX 7

#define LCD_RS A0
#define LCD_EN A1
#define LCD_D4 2
#define LCD_D5 3
#define LCD_D6 4
#define LCD_D7 5

// ─── Object Initialization ───
SoftwareSerial fpSerial(FP_RX, FP_TX);
Adafruit_Fingerprint finger = Adafruit_Fingerprint(&fpSerial);
LiquidCrystal lcd(LCD_RS, LCD_EN, LCD_D4, LCD_D5, LCD_D6, LCD_D7);

// ─── State Machine ───
enum State {
  STATE_IDLE,
  STATE_ENROLLING_STEP1, // Waiting for first finger placement
  STATE_ENROLLING_STEP2, // Waiting for finger removal
  STATE_ENROLLING_STEP3  // Waiting for second finger placement
};

State currentState = STATE_IDLE;
int enrollId = -1;
unsigned long lastScanTime = 0;
const unsigned long SCAN_INTERVAL = 1500; // ms between scans
unsigned long lcdMessageTime = 0;
const unsigned long LCD_MSG_DURATION =
    3000; // ms to show messages before returning to idle
bool showingMessage = false;
String serialBuffer = "";

// ─── Setup ───
void setup() {
  Serial.begin(9600);
  while (!Serial)
    ;

  lcd.begin(16, 2);
  lcdShowIdle();

  fpSerial.begin(57600);
  finger.begin(57600);

  if (finger.verifyPassword()) {
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Sensor Found!");
    lcd.setCursor(0, 1);
    lcd.print("System Ready");
    Serial.println("{\"event\":\"ready\"}");
    delay(1500);
    lcdShowIdle();
  } else {
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Sensor ERROR!");
    lcd.setCursor(0, 1);
    lcd.print("Check wiring");
    Serial.println("{\"event\":\"error\",\"msg\":\"sensor_not_found\"}");
    while (1) {
      delay(1000);
    } // Halt
  }
}

// ─── Main Loop ───
void loop() {
  // Handle incoming serial commands
  handleSerialInput();

  // Auto-return LCD to idle after message timeout
  if (showingMessage && (millis() - lcdMessageTime > LCD_MSG_DURATION)) {
    showingMessage = false;
    if (currentState == STATE_IDLE) {
      lcdShowIdle();
    }
  }

  // State machine
  switch (currentState) {
  case STATE_IDLE:
    if (millis() - lastScanTime > SCAN_INTERVAL) {
      lastScanTime = millis();
      scanFingerprint();
    }
    break;

  case STATE_ENROLLING_STEP1:
    enrollStep1();
    break;

  case STATE_ENROLLING_STEP2:
    enrollStep2();
    break;

  case STATE_ENROLLING_STEP3:
    enrollStep3();
    break;
  }
}

// ─── Serial Command Handler ───
void handleSerialInput() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialBuffer.length() > 0) {
        parseCommand(serialBuffer);
        serialBuffer = "";
      }
    } else {
      serialBuffer += c;
    }
  }
}

void parseCommand(String json) {
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, json);
  if (err)
    return;

  const char *cmd = doc["cmd"];
  if (!cmd)
    return;

  if (strcmp(cmd, "enroll") == 0) {
    int id = doc["id"] | -1;
    if (id > 0 && id < 128) {
      startEnrollment(id);
    } else {
      Serial.println("{\"event\":\"error\",\"msg\":\"invalid_id\"}");
    }
  } else if (strcmp(cmd, "delete") == 0) {
    int id = doc["id"] | -1;
    if (id > 0) {
      deleteFingerprint(id);
    } else {
      Serial.println("{\"event\":\"error\",\"msg\":\"invalid_id\"}");
    }
  } else if (strcmp(cmd, "display") == 0) {
    const char *name = doc["name"];
    if (name) {
      lcdShowName(name);
    }
  }
}

// ─── Fingerprint Scanning (Idle Mode) ───
void scanFingerprint() {
  int result = finger.getImage();
  if (result != FINGERPRINT_OK)
    return; // No finger present or error

  result = finger.image2Tz();
  if (result != FINGERPRINT_OK) {
    lcdShowMessage("Image Error", "Try Again");
    return;
  }

  result = finger.fingerFastSearch();
  if (result == FINGERPRINT_OK) {
    // Match found!
    int foundId = finger.fingerID;
    int confidence = finger.confidence;

    // Send match event to PC
    StaticJsonDocument<128> doc;
    doc["event"] = "match";
    doc["id"] = foundId;
    doc["confidence"] = confidence;
    doc["timestamp"] = "auto";
    String output;
    serializeJson(doc, output);
    Serial.println(output);

    // Show temporary match message (PC will send display command with name)
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("ID: ");
    lcd.print(foundId);
    lcd.setCursor(0, 1);
    lcd.print("Verifying...");
    showingMessage = true;
    lcdMessageTime = millis();

    delay(1500); // Debounce - wait for finger to be removed
  } else if (result == FINGERPRINT_NOTFOUND) {
    // No match
    Serial.println("{\"event\":\"no_match\"}");
    lcdShowMessage("Unknown Finger", "Not Registered");
    delay(1500); // Debounce
  }
}

// ─── Enrollment Process ───
void startEnrollment(int id) {
  enrollId = id;
  currentState = STATE_ENROLLING_STEP1;
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Enroll ID: ");
  lcd.print(id);
  lcd.setCursor(0, 1);
  lcd.print("Place finger...");
  Serial.println("{\"event\":\"enroll_started\"}");
}

void enrollStep1() {
  int result = finger.getImage();
  if (result != FINGERPRINT_OK)
    return;

  result = finger.image2Tz(1);
  if (result != FINGERPRINT_OK) {
    lcdShowMessage("Image Error", "Try Again");
    delay(1000);
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Enroll ID: ");
    lcd.print(enrollId);
    lcd.setCursor(0, 1);
    lcd.print("Place finger...");
    return;
  }

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Got it!");
  lcd.setCursor(0, 1);
  lcd.print("Remove finger");
  currentState = STATE_ENROLLING_STEP2;
  delay(1000);
}

void enrollStep2() {
  int result = finger.getImage();
  if (result == FINGERPRINT_NOFINGER) {
    // Finger removed, proceed
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Place same");
    lcd.setCursor(0, 1);
    lcd.print("finger again...");
    currentState = STATE_ENROLLING_STEP3;
    delay(500);
  }
}

void enrollStep3() {
  int result = finger.getImage();
  if (result != FINGERPRINT_OK)
    return;

  result = finger.image2Tz(2);
  if (result != FINGERPRINT_OK) {
    lcdShowMessage("Image Error", "Try Again");
    delay(1000);
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Place same");
    lcd.setCursor(0, 1);
    lcd.print("finger again...");
    return;
  }

  // Create model
  result = finger.createModel();
  if (result != FINGERPRINT_OK) {
    lcdShowMessage("Prints didn't", "match! Retry");
    Serial.println("{\"event\":\"error\",\"msg\":\"enroll_mismatch\"}");
    delay(1500);
    currentState = STATE_IDLE;
    enrollId = -1;
    lcdShowIdle();
    return;
  }

  // Store model
  result = finger.storeModel(enrollId);
  if (result == FINGERPRINT_OK) {
    lcdShowMessage("Enrolled!", "Success!");

    StaticJsonDocument<128> doc;
    doc["event"] = "enrolled";
    doc["id"] = enrollId;
    String output;
    serializeJson(doc, output);
    Serial.println(output);
  } else {
    lcdShowMessage("Store Error", "Try Again");
    Serial.println("{\"event\":\"error\",\"msg\":\"store_failed\"}");
  }

  delay(1500);
  currentState = STATE_IDLE;
  enrollId = -1;
  lcdShowIdle();
}

// ─── Delete Fingerprint ───
void deleteFingerprint(int id) {
  int result = finger.deleteModel(id);
  if (result == FINGERPRINT_OK) {
    lcdShowMessage("Deleted ID:", String(id));

    StaticJsonDocument<128> doc;
    doc["event"] = "deleted";
    doc["id"] = id;
    String output;
    serializeJson(doc, output);
    Serial.println(output);
  } else {
    lcdShowMessage("Delete Fail", "ID: " + String(id));
    Serial.println("{\"event\":\"error\",\"msg\":\"delete_failed\"}");
  }
  delay(1500);
  lcdShowIdle();
}

// ─── LCD Helper Functions ───
void lcdShowIdle() {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Scan Finger...");
  lcd.setCursor(0, 1);
  lcd.print("Attendance Sys");
  showingMessage = false;
}

void lcdShowName(const char *name) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Welcome!");
  lcd.setCursor(0, 1);
  // Truncate name to 16 chars for LCD
  char buf[17];
  strncpy(buf, name, 16);
  buf[16] = '\0';
  lcd.print(buf);
  showingMessage = true;
  lcdMessageTime = millis();
}

void lcdShowMessage(String line1, String line2) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(line1.substring(0, 16));
  lcd.setCursor(0, 1);
  lcd.print(line2.substring(0, 16));
  showingMessage = true;
  lcdMessageTime = millis();
}
