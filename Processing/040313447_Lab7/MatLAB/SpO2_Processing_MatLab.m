clc;
clear;
close all;
load PPG5.mat

Fs = 500;
Ts = 1/Fs;
n = 1:1:length(RedPPG);
t = n*Ts; 

RedDelta = 0.003;
IRDelta = 0.003;
[MAXR, MINR] = peakdet(RedPPG,0.0003);
[MAXIR, MINIR] = peakdet(IRPPG,0.0003);

figure; 
subplot(2,1,1),plot(t,RedPPG,'-r'); grid on; hold on;
plot(MAXR(:,1)*Ts,MAXR(:,2),'*b');
plot(MINR(:,1)*Ts,MINR(:,2),'+b');
ylabel('Amplitude');xlabel('Time(s)');
title('Red PPG');

subplot(2,1,2),plot(t,IRPPG,'-b'); grid on; hold on;
plot(MAXIR(:,1)*Ts,MAXIR(:,2),'*r');
plot(MINIR(:,1)*Ts,MINIR(:,2),'+r');
ylabel('Amplitude');xlabel('Time(s)');
title('IR PPG');

%% Pulse oximetry Algorithm 1
ACred = MAXR(:,2)-MINR(:,2);
DCred = (MAXR(:,2)+MINR(:,2))/2;
ACir = MAXIR(:,2)-MINIR(:,2);
DCir = (MAXIR(:,2)+MINIR(:,2))/2;
RedAbsorption1 = ACred./DCred;
IRAbsorption1 = ACir./DCir;
R1 = RedAbsorption1./IRAbsorption1;
SpO2_1 = 110 - (25*(R1));

%% Pulse Oximetry Algorithm 2
RedAbsorption2 = log(MAXR(:,2)./MINR(:,2));
IRAbsorption2 = log(MAXIR(:,2)./MINIR(:,2));
R2 = RedAbsorption1./IRAbsorption1;
SpO2_2 = 110 - (25*(R1));

%% Pulse Rate
Tn = diff(MAXIR(:,1))*Ts;
PR = 60./Tn;

%% Perfusion Index 
PI = (ACir./DCir)*100;clc;
clear;
close all;
load PPG5.mat

Fs = 500;
Ts = 1/Fs;
n = 1:1:length(RedPPG);
t = n*Ts; 

RedDelta = 0.1;
IRDelta = 0.1;
[MAXR, MINR] = peakdet(RedPPG,0.0003);
[MAXIR, MINIR] = peakdet(IRPPG,0.0003);

figure; 
subplot(2,1,1),plot(t,RedPPG,'-r'); grid on; hold on;
plot(MAXR(:,1)*Ts,MAXR(:,2),'*b');
plot(MINR(:,1)*Ts,MINR(:,2),'+b');
ylabel('Amplitude');xlabel('Time(s)');
title('Red PPG');

subplot(2,1,2),plot(t,IRPPG,'-b'); grid on; hold on;
plot(MAXIR(:,1)*Ts,MAXIR(:,2),'*r');
plot(MINIR(:,1)*Ts,MINIR(:,2),'+r');
ylabel('Amplitude');xlabel('Time(s)');
title('IR PPG');

%% Pulse oximetry Algorithm 1
ACred = MAXR(:,2)-MINR(:,2);
DCred = (MAXR(:,2)+MINR(:,2))/2;
ACir = MAXIR(:,2)-MINIR(:,2);
DCir = (MAXIR(:,2)+MINIR(:,2))/2;
RedAbsorption1 = ACred./DCred;
IRAbsorption1 = ACir./DCir;
R1 = RedAbsorption1./IRAbsorption1;
SpO2_1 = 110 - (25*(R1));

%% Pulse Oximetry Algorithm 2
RedAbsorption2 = log(MAXR(:,2)./MINR(:,2));
IRAbsorption2 = log(MAXIR(:,2)./MINIR(:,2));
R2 = RedAbsorption1./IRAbsorption1;
SpO2_2 = 110 - (25*(R1));

%% Pulse Rate
Tn = diff(MAXIR(:,1))*Ts;
PR = 60./Tn;

%% Perfusion Index 
PI = (ACir./DCir)*100;