#import <Foundation/Foundation.h>
#include "../Hikari.h"

HIKARI_PROTECT BOOL HikariARCDecision(NSString *value) {
  NSString *lower = [value lowercaseString];
  return lower.length == 4 && [lower hasPrefix:@"test"];
}

HIKARI_PROTECT NSString *HikariARCObject(NSString *value) {
  @try {
    if (!value.length)
      @throw [NSException exceptionWithName:@"HikariARC" reason:nil userInfo:nil];
    return [value lowercaseString];
  } @catch (NSException *exception) {
    return value;
  }
}

int main(void) {
  @autoreleasepool {
    return !HikariARCDecision(@"TEST") || HikariARCDecision(@"other") ||
           ![HikariARCObject(@"TEST") isEqualToString:@"test"] ||
           HikariARCObject(@"").length != 0;
  }
}
