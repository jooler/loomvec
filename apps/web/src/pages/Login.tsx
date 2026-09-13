import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { useNavigate } from 'react-router';
import { z } from 'zod';
import { toast } from 'sonner';
import { useAuth } from '@/auth';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';

const loginSchema = z.object({
  username: z.string().min(1, '请输入用户名'),
});

type LoginValues = z.infer<typeof loginSchema>;

/** dev 登录页（P0）：签发测试 JWT。OIDC 登录位 P4 接入。 */
export function LoginPage() {
  const { loginDev } = useAuth();
  const navigate = useNavigate();
  const form = useForm<LoginValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { username: 'dev' },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    try {
      await loginDev(values.username);
      toast.success('登录成功');
      navigate('/');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '登录失败');
    }
  });

  return (
    <div className="grid min-h-svh place-items-center bg-muted/40 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>登录 LoomVec</CardTitle>
          <CardDescription>dev 模式免密登录，签发测试 JWT</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="username">用户名（dev 模式免密）</Label>
              <Input
                id="username"
                placeholder="用户名"
                autoComplete="username"
                {...form.register('username')}
              />
              {form.formState.errors.username && (
                <p className="text-sm text-destructive">
                  {form.formState.errors.username.message}
                </p>
              )}
            </div>
            <Button type="submit" className="w-full" disabled={form.formState.isSubmitting}>
              {form.formState.isSubmitting ? '登录中…' : 'dev 登录'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
