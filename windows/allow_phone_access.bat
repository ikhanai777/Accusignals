@echo off
REM Opens TCP 8765 on PRIVATE networks only, so a phone on your home Wi-Fi can reach the dashboard.
REM Right-click -> "Run as administrator". Remove with:
REM   netsh advfirewall firewall delete rule name="Accusignals dashboard"
net session >nul 2>&1 || (echo Please run this as administrator. & exit /b 1)
netsh advfirewall firewall add rule name="Accusignals dashboard" dir=in action=allow protocol=TCP localport=8765 profile=private
