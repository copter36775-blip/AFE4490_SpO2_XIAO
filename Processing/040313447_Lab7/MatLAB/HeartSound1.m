load HSound;
 t=(0:length(HSwave)-1)/Fs;
 figure; subplot(2,1,1); plot(t,HSwave);
 ylabel('Amplitude(V)');xlabel('Time(s)');
 title('Heart Sound Signal');
 Y = abs(fft(HSwave));
 N =length(Y);
 Y =Y(1:floor(N/2));
 Y(1)=0; 
 f1=(0:N/2-1)*Fs/N;
 subplot(2,1,2);plot(f1,Y);
 ylabel('Magnitude'); xlabel('Frequency (Hz)');
 title('Frequency component of heart sound');
 
 