#import <Foundation/Foundation.h>

@interface HikariProbe : NSObject
+ (NSString *)message;
- (int)value:(int)n;
@end
@implementation HikariProbe
+ (NSString *)message { return @"hikari-objc-pac"; }
- (int)value:(int)n { return n + 5; }
@end

@interface HikariChild : HikariProbe
@end
@implementation HikariChild
- (int)value:(int)n { return [super value:n] + 1; }
@end

@interface HikariEmpty : NSObject
@end
@implementation HikariEmpty
@end

int main(void) {
  @autoreleasepool {
    HikariProbe *p = [HikariProbe new];
    HikariChild *child = [HikariChild new];
    HikariEmpty *empty = [HikariEmpty new];
    return [p value:37] != 42 || [child value:37] != 43 || !empty ||
           ![[HikariProbe message] isEqualToString:@"hikari-objc-pac"];
  }
}
