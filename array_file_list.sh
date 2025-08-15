#!/bin/bash

folder='/mnt/user/backup/Yellowstone/'

# Define a function to handle errors
error_handler() {
    echo "An error occurred. Exiting script."
    # using curl (10 second timeout, retry up to 5 times):
    curl -m 10 --retry 5 https://healthchecks.waun.net/ping/f02d8f20-6817-41a0-9c43-c8071df1c934/fail
    exit 1
}

# Set the trap to catch errors and call the error_handler function
trap 'error_handler' ERR

# using curl (10 second timeout, retry up to 5 times):
curl -m 10 --retry 5 https://healthchecks.waun.net/ping/f02d8f20-6817-41a0-9c43-c8071df1c934/start

for disk in $(ls /mnt | grep -e 'disk[0-9]' | sed 's/\///g')
do 
  echo $disk
  find /mnt/$disk -type f -fprintf $folder/filelist.$disk.txt "%P\r\n"
done

echo cache
find /mnt/cache -type f -fprintf $folder/filelist.cache.txt "%P\r\n"

echo cache_appdata
find /mnt/cache_appdata -type f -fprintf $folder/filelist.cache_appdata.txt "%P\r\n"

tar --remove-files -czvf $folder/array_filelist.tar.gz $folder/filelist*.txt

# using curl (10 second timeout, retry up to 5 times):
curl -m 10 --retry 5 https://healthchecks.waun.net/ping/f02d8f20-6817-41a0-9c43-c8071df1c934
